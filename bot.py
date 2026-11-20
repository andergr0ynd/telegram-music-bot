"""Local Telegram music library. Python 3.10+, no dependencies."""
import json
import logging
import os
from functools import lru_cache
from pathlib import Path
import re
import sqlite3
import time
import threading
import unicodedata
import urllib.error
import urllib.request
import uuid
import hashlib

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('music_bot')
TRANSLIT = str.maketrans(dict(zip(
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    'a b v g d e yo zh z i y k l m n o p r s t u f kh ts ch sh shch _ y _ e yu ya'.split()
)))
TRANSLIT[ord('ъ')] = TRANSLIT[ord('ь')] = ''
WORD = re.compile(r'[^\W_]+')


@lru_cache(maxsize=4096)
def search_tokens(value):
    return tuple(WORD.findall(unicodedata.normalize('NFKC', value).casefold()))


@lru_cache(maxsize=8192)
def transliterations(word):
    return {word.translate(TRANSLIT), word.replace('ё', 'е').translate(TRANSLIT)} - {''}


def one_edit(left, right):
    if len(left) > len(right):
        left, right = right, left
    if len(right) - len(left) > 1:
        return False
    for index, char in enumerate(left):
        if char != right[index]:
            if len(left) != len(right):
                return left[index:] == right[index + 1:]
            return (left[index + 1:] == right[index + 1:]
                    or left[index:index + 2] == right[index:index + 2][::-1]
                    and left[index + 2:] == right[index + 2:])
    return True


def search_score(query, title, performer, search):
    words = search_tokens(query)[:20]
    if not words:
        return 0
    title_words = search_tokens(title)
    phrase = ' '.join(words)
    if words == title_words:
        base = 0
    elif phrase in ' '.join(title_words):
        base = 1
    elif phrase in ' '.join(search_tokens(performer)):
        base = 2
    else:
        base = 3

    indexed = search_tokens(search)
    normalized = ' '.join(indexed)
    quality = 0
    for word in words:
        if word in normalized:
            continue
        aliases = transliterations(word)
        if any(alias in candidate for token in indexed
               for candidate in transliterations(token) for alias in aliases):
            quality = max(quality, 1)
        elif len(word) >= 4 and any(
            one_edit(word, token) or any(one_edit(alias, candidate)
                                         for candidate in transliterations(token) for alias in aliases)
            for token in indexed
        ):
            quality = 2
        else:
            return None
    return quality * 4 + base


def load_token():
    config = ROOT / '.env'
    if config.exists():
        for line in config.read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key == 'BOT_TOKEN':
                os.environ.setdefault(key, value.strip().strip('\"\''))
    token = os.environ.get('BOT_TOKEN', '').strip()
    if not token or token == 'PASTE_TOKEN_HERE':
        raise ValueError('Укажите BOT_TOKEN в файле .env рядом с bot.py.')
    return token


class APIError(Exception):
    def __init__(self, code, description, retry_after=5):
        super().__init__(f'Telegram {code}: {description}')
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **payload):
        request = urllib.request.Request(
            self.base + method, data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                data = json.load(error)
            except (ValueError, OSError):
                raise APIError(error.code, 'Ошибка HTTP') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, 'Нет связи с Telegram') from None
        if not data.get('ok'):
            raise APIError(data.get('error_code', 0), data.get('description', 'Ошибка'),
                           data.get('parameters', {}).get('retry_after', 5))
        return data['result']

    def cache_audio(self, file_id, destination):
        destination = Path(destination)
        if destination.is_file():
            return destination
        info = self.call('getFile', file_id=file_id)
        if info.get('file_size', 0) > 20 * 1024 * 1024:
            raise APIError(400, 'Для восстановления файл должен быть меньше 20 МБ')
        url = self.base.replace('/bot', '/file/bot', 1) + info['file_path']
        try:
            with urllib.request.urlopen(url, timeout=40) as response:
                content = response.read(20 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, 'Не удалось скачать аудио для восстановления') from None
        if len(content) > 20 * 1024 * 1024:
            raise APIError(400, 'Файл больше 20 МБ')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.part')
        try:
            temporary.write_bytes(content)
            temporary.replace(destination)
        except OSError:
            raise APIError(0, 'Не удалось сохранить музыку на диск') from None
        return destination

    def upload_audio(self, chat, path, title, performer):
        path = Path(path)
        content = path.read_bytes()
        boundary = uuid.uuid4().hex
        body = bytearray()
        for key, value in {'chat_id': chat, 'title': title, 'performer': performer,
                           'caption': 'Аудио восстановлено для inline-поиска'}.items():
            body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        suffix = path.suffix.lower()
        suffix = suffix if suffix in ('.mp3', '.m4a') else '.mp3'
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="track{suffix}"\r\nContent-Type: {"audio/mp4" if suffix == ".m4a" else "audio/mpeg"}\r\n\r\n'.encode())
        body.extend(content)
        body.extend(f'\r\n--{boundary}--\r\n'.encode())
        request = urllib.request.Request(self.base + 'sendAudio', data=bytes(body),
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                result = json.load(error)
            except (ValueError, OSError):
                raise APIError(error.code, 'Telegram отклонил восстановление аудио') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, 'Не удалось загрузить восстановленное аудио') from None
        if not result.get('ok'):
            raise APIError(result.get('error_code', 400),
                           result.get('description', 'Ошибка восстановления'),
                           result.get('parameters', {}).get('retry_after', 5))
        return result['result']['audio']


class Library:
    def __init__(self, path, music_dir=None):
        self.music_dir = Path(music_dir) if music_dir else ROOT / 'music' / Path(path).stem
        self.inline_valid = {}
        self.db = sqlite3.connect(path)
        self.db.create_function('search_score', 4, search_score, deterministic=True)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
                unique_id TEXT NOT NULL, file_id TEXT NOT NULL,
                title TEXT NOT NULL, performer TEXT NOT NULL, search TEXT NOT NULL,
                UNIQUE(owner, unique_id));
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, owner INTEGER NOT NULL,
                chat INTEGER NOT NULL, track INTEGER);
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(tracks)')}
        with self.db:
            if 'local_path' not in columns:
                self.db.execute("ALTER TABLE tracks ADD COLUMN local_path TEXT NOT NULL DEFAULT ''")
            if 'deleted' not in columns:
                self.db.execute("ALTER TABLE tracks ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")

    def track(self, owner, track_id):
        return self.db.execute('SELECT * FROM tracks WHERE owner=? AND id=? AND deleted=0',
                               (owner, track_id)).fetchone()

    def enqueue(self, kind, owner, chat, track_id=None):
        self.db.execute('INSERT INTO jobs(kind, owner, chat, track) VALUES (?, ?, ?, ?)',
                        (kind, owner, chat, track_id))

    def own_tracks(self, owner):
        return self.db.execute('SELECT * FROM tracks WHERE owner=? AND deleted=0 ORDER BY id',
                               (owner,)).fetchall()

    def edit(self, owner, track_id, title, performer, chat):
        row = self.track(owner, track_id)
        if row is None:
            return False
        prefix = f'{row["title"]} {row["performer"]} '.casefold()
        extra = row['search'].removeprefix(prefix)
        with self.db:
            self.db.execute('UPDATE tracks SET title=?, performer=?, search=? WHERE id=?',
                            (title, performer, f'{title} {performer} '.casefold() + extra, track_id))
            self.enqueue('upload', owner, chat, track_id)
        return True

    def delete(self, owner, track_id, chat):
        with self.db:
            changed = self.db.execute(
                "UPDATE tracks SET deleted=1, title='', performer='', search='', file_id='' "
                'WHERE owner=? AND id=? AND deleted=0', (owner, track_id)).rowcount
            if changed:
                self.enqueue('delete', owner, chat, track_id)
        return bool(changed)

    def remove_audio(self, owner, track_id):
        row = self.db.execute('SELECT * FROM tracks WHERE owner=? AND id=? AND deleted=1',
                              (owner, track_id)).fetchone()
        if row is None:
            return
        for suffix in ('.mp3', '.m4a'):
            destination = self.audio_path(row, suffix)
            destination.unlink(missing_ok=True)
            destination.with_suffix(suffix + '.part').unlink(missing_ok=True)
        with self.db:
            self.db.execute("UPDATE tracks SET local_path='' WHERE id=? AND deleted=1", (track_id,))

    def audio_path(self, row, suffix):
        digest = hashlib.sha256(row['unique_id'].encode()).hexdigest()[:24]
        return self.music_dir / str(row['owner']) / (digest + suffix)

    def cache(self, api, row):
        suffix = '.m4a' if row['title'].lower().endswith('.m4a') else '.mp3'
        destination = self.audio_path(row, suffix)
        for existing_suffix in ('.mp3', '.m4a'):
            existing = self.audio_path(row, existing_suffix)
            if existing.is_file():
                destination = existing
                break
        path = api.cache_audio(row['file_id'], destination)
        with self.db:
            self.db.execute('UPDATE tracks SET local_path=? WHERE id=?', (str(path.resolve()), row['id']))
        return path

    def upload(self, api, row, chat):
        path = self.cache(api, row)
        audio = api.upload_audio(chat, path, row['title'].strip() or 'Без названия', row['performer'])
        result = {'type': 'audio', 'id': str(row['id']), 'audio_file_id': audio['file_id'],
                  'title': row['title'].strip() or 'Без названия', 'performer': row['performer']}
        api.call('savePreparedInlineMessage', user_id=row['owner'], result=result, allow_user_chats=True)
        with self.db:
            updated = self.db.execute(
                'UPDATE tracks SET file_id=? WHERE id=? AND deleted=0 AND file_id=? '
                'AND title=? AND performer=?',
                (audio['file_id'], row['id'], row['file_id'], row['title'], row['performer'])).rowcount
        if not updated:
            return None
        self.inline_valid[audio['file_id']] = True
        return audio

    def save(self, owner, audio, caption='', chat=None):
        title = (audio.get('title') or '').strip() or (audio.get('file_name') or '').strip() or 'Без названия'
        performer = audio.get('performer', '')
        search = f'{title} {performer} {audio.get("file_name", "")} {caption}'.casefold()
        with self.db:
            self.db.execute('''INSERT INTO tracks(owner, unique_id, file_id, title, performer, search)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner, unique_id) DO UPDATE SET
                file_id=excluded.file_id, title=excluded.title,
                performer=excluded.performer, search=excluded.search, deleted=0''',
                (owner, audio['file_unique_id'], audio['file_id'], title, performer, search))
            if chat is not None:
                row = self.db.execute('SELECT id FROM tracks WHERE owner=? AND unique_id=?',
                                      (owner, audio['file_unique_id'])).fetchone()
                self.enqueue('upload', owner, chat, row['id'])
        return title

    def search(self, owner, query, offset=0):
        where = 'deleted=0'
        params = [query]
        if owner is not None:
            where += ' AND owner=?'
            params.append(owner)
        scope = 'WHERE duplicate_rank=1' if owner is None else ''
        return self.db.execute(f'''
            WITH scored AS (
                SELECT *, search_score(?, title, performer, search) AS score
                FROM tracks WHERE {where}
            ), ranked AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY unique_id ORDER BY score, id DESC)
                    AS duplicate_rank FROM scored WHERE score IS NOT NULL
            )
            SELECT * FROM ranked {scope} ORDER BY score, id DESC LIMIT 51 OFFSET ?
        ''', [*params, offset]).fetchall()

    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def advance(self, offset):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (str(offset),))


HELP = ('Отправьте мне музыку как аудио с плеером Telegram — я добавлю её в вашу библиотеку.\n'
        'Затем в любом чате напишите @{username} название или исполнителя.\n'
        'Пустой запрос покажет последние треки. Подпись к аудио тоже участвует в поиске.\n\n'
        '/library — последние 20 общих треков\n'
        '/mytracks — мои треки и команды управления')


def process_job(api, library, username):
    job = library.db.execute('SELECT * FROM jobs ORDER BY id LIMIT 1').fetchone()
    if job is None:
        return False
    kind, owner, chat, track_id = (job[key] for key in ('kind', 'owner', 'chat', 'track'))
    try:
        if kind == 'delete':
            library.remove_audio(owner, track_id)
            text = f'Удаление трека #{track_id} завершено.'
        elif kind == 'upload':
            row = library.track(owner, track_id)
            text = ''
            if row is not None and library.upload(api, row, chat):
                text = (f'Трек #{track_id} готов к inline-поиску и сохранён в папку: {row["title"]}\n'
                        f'Поиск: @{username} название')
        else:
            rows = library.own_tracks(owner)
            completed = 0
            failures = []
            for index, row in enumerate(rows):
                current = library.track(owner, row['id'])
                if current is None:
                    continue
                try:
                    if kind == 'cache':
                        library.cache(api, current)
                        completed += 1
                    elif library.upload(api, current, chat):
                        completed += 1
                except (APIError, OSError) as error:
                    failures.append(f'#{row["id"]} {row["title"]}: {error}')
                    if isinstance(error, APIError) and error.code == 429:
                        time.sleep(max(1, error.retry_after))
                        continue
                if kind == 'repair' and index + 1 < len(rows):
                    time.sleep(1)
            text = f'Обновлено {completed} из {len(rows)}.'
            if failures:
                text += '\nОшибки:\n' + '\n'.join(failures)
    except (APIError, OSError) as error:
        text = f'Не удалось обработать трек #{track_id}: {error}'
        if kind == 'upload':
            text += f'\nПовторить подготовку: /repair {track_id}'
        if isinstance(error, APIError) and error.code == 429:
            time.sleep(max(1, error.retry_after))
    with library.db:
        library.db.execute('DELETE FROM jobs WHERE id=?', (job['id'],))
    if text:
        try:
            api.call('sendMessage', chat_id=chat, text=text[:4000])
        except APIError as error:
            LOG.warning('Не удалось сообщить о задаче %s: %s', job['id'], error)
    return True


def run_jobs(api, path, music_dir, username, stop):
    library = Library(path, music_dir)
    try:
        while not stop.is_set():
            try:
                if not process_job(api, library, username):
                    stop.wait(0.5)
            except sqlite3.Error:
                LOG.exception('Ошибка фоновой очереди')
                stop.wait(2)
    finally:
        library.db.close()


def parse_track_id(value):
    if value.isascii() and value.isdecimal() and len(value) <= 18:
        return int(value)
    return 0


def handle(update, api, library, username):
    if 'inline_query' in update:
        query = update['inline_query']
        raw_offset = query.get('offset', '')
        offset = min(int(raw_offset), 1000000) if raw_offset.isdecimal() else 0
        rows = library.search(None, query['query'], offset)
        results = [{'type': 'audio', 'id': str(row['id']), 'audio_file_id': row['file_id'],
                           'title': row['title'].strip() or 'Без названия',
                           'performer': row['performer']}
                   for row in rows[:50]]
        options = dict(inline_query_id=query['id'], is_personal=True, cache_time=0,
                       next_offset=str(offset + 50) if len(rows) > 50 else '')
        try:
            api.call('answerInlineQuery', results=results, **options)
        except APIError as error:
            if error.code != 400 or 'AUDIO_TITLE_EMPTY' not in str(error):
                raise
            valid = []
            invalid = []
            for row, result in zip(rows[:50], results):
                fid = row['file_id']
                if fid not in library.inline_valid:
                    try:
                        api.call('savePreparedInlineMessage', user_id=query['from']['id'],
                                 result=result, allow_user_chats=True)
                        library.inline_valid[fid] = True
                    except APIError as check_error:
                        if check_error.code != 400 or 'AUDIO_TITLE_EMPTY' not in str(check_error):
                            raise
                        library.inline_valid[fid] = False
                if library.inline_valid[fid]:
                    valid.append(result)
                else:
                    invalid.append(row)
            hints = [
                {'type': 'article', 'id': str(row['id']),
                 'title': row['title'].strip() or 'Без названия',
                 'description': 'Откройте бота и отправьте /repair для восстановления аудио',
                 'input_message_content': {'message_text': 'Для восстановления трека откройте @' + username + ' и отправьте /repair.'}}
                for row in invalid]
            # Keep one result type per answer so Telegram clients display the list consistently.
            api.call('answerInlineQuery', results=valid or hints, **options)
            LOG.info('Inline: исправных треков %s, требуют /repair: %s.', len(valid), len(invalid))
        return
    message = update.get('message', {})
    if message.get('chat', {}).get('type') != 'private' or message.get('from', {}).get('is_bot'):
        return
    owner = message['from']['id']
    chat = message['chat']['id']
    parts = message.get('text', '').split(maxsplit=1)
    command_name = parts[0].split('@')[0] if parts else ''
    argument = parts[1].strip() if len(parts) > 1 else ''
    if 'audio' in message:
        title = library.save(owner, message['audio'], message.get('caption', ''), chat=chat)
        text = f'Трек принят: {title}. Сообщу, когда он будет готов.'
    elif command_name in ('/repair', '/cache'):
        track_id = parse_track_id(argument)
        if command_name == '/repair' and argument:
            if not track_id or library.track(owner, track_id) is None:
                text = 'Трек не найден. Номера ваших треков: /mytracks'
            else:
                with library.db:
                    library.enqueue('upload', owner, chat, track_id)
                text = f'Трек #{track_id} поставлен в очередь на восстановление.'
        else:
            with library.db:
                library.enqueue(command_name[1:], owner, chat)
            text = 'Задача поставлена в очередь. Сообщу результат.'
    elif command_name == '/mytracks':
        page = parse_track_id(argument) or 1
        rows = library.search(owner, '', (page - 1) * 10) if page <= 100000 else []
        commands = ('/edittrack номер | название | исполнитель\n'
                    '/deletetrack номер\n'
                    '/repair номер')
        if rows:
            tracks = '\n'.join(f'#{row["id"]} {row["performer"]} — {row["title"]}' if row['performer']
                                else f'#{row["id"]} {row["title"]}' for row in rows[:10])
            navigation = []
            if page > 1:
                navigation.append(f'Предыдущая страница: /mytracks {page - 1}')
            if len(rows) > 10:
                navigation.append(f'Следующая страница: /mytracks {page + 1}')
            text = '\n\n'.join(filter(None, (commands, '\n'.join(navigation), tracks)))
        else:
            text = 'На этой странице нет треков. Открыть первую страницу: /mytracks' if page > 1 else 'Вы ещё не загрузили треки.'
    elif command_name == '/edittrack':
        fields = [field.strip() for field in argument.split('|', 2)]
        if len(fields) != 3 or not parse_track_id(fields[0]) or not fields[1]:
            text = 'Формат: /edittrack номер | название | исполнитель. Исполнителя можно оставить пустым.'
        elif not library.edit(owner, parse_track_id(fields[0]), fields[1], fields[2], chat):
            text = 'Трек не найден. Номера ваших треков: /mytracks'
        else:
            text = 'Название и исполнитель обновлены. Трек поставлен в очередь на подготовку.'
    elif command_name == '/deletetrack':
        track_id = parse_track_id(argument)
        if not track_id or not library.delete(owner, track_id, chat):
            text = 'Трек не найден. Номера ваших треков: /mytracks'
        else:
            text = f'Трек #{track_id} удалён из вашей библиотеки. Локальный файл поставлен в очередь на удаление.'
    elif command_name in ('/library', '/search'):
        command, _, search_query = message['text'].partition(' ')
        searching = command.split('@')[0] == '/search'
        if searching and not search_query.strip():
            text = f'Введите /search название или исполнитель.\nИли используйте @{username} название в любом чате.'
        else:
            rows = library.search(None, search_query if searching else '')[:20]
            text = '\n'.join(f'{r["performer"] + " — " if r["performer"] else ""}{r["title"]}' for r in rows)
            text = (('Найденные треки:\n' if searching else 'Последние общие треки:\n') + text) if text else 'Ничего не найдено. Отправьте музыку, чтобы пополнить общую библиотеку.'
    elif 'document' in message:
        text = 'Пришлите файл как музыку с плеером Telegram (MP3/M4A), а не как документ.'
    else:
        text = HELP.format(username=username)
    api.call('sendMessage', chat_id=chat, text=text[:4000])


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    api = Telegram(load_token())
    me = api.call('getMe')
    if api.call('getWebhookInfo').get('url'):
        raise ValueError('У бота установлен webhook. Используйте отдельного бота или удалите webhook перед запуском.')
    library = Library(ROOT / f'library_{me["id"]}.sqlite3')
    library.music_dir.mkdir(parents=True, exist_ok=True)
    LOG.info('Бот @%s запущен (версия 1.8, общая библиотека). Музыка: %s. Для остановки нажмите Ctrl+C.', me['username'], library.music_dir)
    if not me.get('supports_inline_queries'):
        LOG.warning('Включите inline в @BotFather: /setinline → @%s → Найти музыку', me['username'])
    stop = threading.Event()
    worker = threading.Thread(target=run_jobs, daemon=True,
                              args=(api, ROOT / f'library_{me["id"]}.sqlite3',
                                    library.music_dir, me['username'], stop))
    worker.start()
    try:
        while True:
            try:
                updates = api.call('getUpdates', offset=library.offset(), timeout=25,
                                   allowed_updates=['message', 'inline_query'])
                for update in updates:
                    try:
                        handle(update, api, library, me['username'])
                    except APIError as error:
                        if 'inline_query' in update and ('query is too old' in str(error) or 'QUERY_ID_INVALID' in str(error)):
                            library.advance(update['update_id'] + 1)
                            continue
                        if error.code in (0, 429) or error.code >= 500:
                            raise
                        LOG.warning('Не удалось обработать событие %s: %s', update['update_id'], error)
                    library.advance(update['update_id'] + 1)
            except APIError as error:
                if error.code in (401, 404, 409):
                    raise
                LOG.warning('%s. Повтор подключения.', error)
                time.sleep(max(1, error.retry_after))
    finally:
        stop.set()
        library.db.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except (APIError, ValueError) as error:
        print(error)
        raise SystemExit(1)
