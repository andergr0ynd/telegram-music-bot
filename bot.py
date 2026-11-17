"""Local Telegram music library. Python 3.10+, no dependencies."""
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
import hashlib

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('music_bot')


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
            raise APIError(error.code, 'Telegram отклонил восстановление аудио') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise APIError(0, 'Не удалось загрузить восстановленное аудио') from None
        if not result.get('ok'):
            raise APIError(400, result.get('description', 'Ошибка восстановления'))
        return result['result']['audio']


class Library:
    def __init__(self, path, music_dir=None):
        self.music_dir = Path(music_dir) if music_dir else ROOT / 'music' / Path(path).stem
        self.inline_valid = {}
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY, owner INTEGER NOT NULL,
                unique_id TEXT NOT NULL, file_id TEXT NOT NULL,
                title TEXT NOT NULL, performer TEXT NOT NULL, search TEXT NOT NULL,
                UNIQUE(owner, unique_id));
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        if 'local_path' not in {row[1] for row in self.db.execute('PRAGMA table_info(tracks)')}:
            with self.db:
                self.db.execute("ALTER TABLE tracks ADD COLUMN local_path TEXT NOT NULL DEFAULT ''")

    def cache(self, api, row):
        suffix = '.m4a' if row['title'].lower().endswith('.m4a') else '.mp3'
        digest = hashlib.sha256(row['unique_id'].encode()).hexdigest()[:24]
        destination = self.music_dir / str(row['owner']) / (digest + suffix)
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
            self.db.execute('UPDATE tracks SET file_id=? WHERE id=?', (audio['file_id'], row['id']))
        self.inline_valid[audio['file_id']] = True
        return audio

    def save(self, owner, audio, caption=''):
        title = (audio.get('title') or '').strip() or (audio.get('file_name') or '').strip() or 'Без названия'
        performer = audio.get('performer', '')
        search = f'{title} {performer} {audio.get("file_name", "")} {caption}'.casefold()
        with self.db:
            self.db.execute('''INSERT INTO tracks(owner, unique_id, file_id, title, performer, search)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(owner, unique_id) DO UPDATE SET
                file_id=excluded.file_id, title=excluded.title,
                performer=excluded.performer, search=excluded.search''',
                (owner, audio['file_unique_id'], audio['file_id'], title, performer, search))
        return title

    def search(self, owner, query, offset=0):
        words = query.casefold().split()[:20]
        filters = ['owner=?'] if owner is not None else [
            'id IN (SELECT MAX(id) FROM tracks GROUP BY unique_id)']
        where = ' AND '.join(filters + ['instr(search, ?) > 0'] * len(words))
        return self.db.execute(f'SELECT * FROM tracks WHERE {where} ORDER BY id DESC LIMIT 51 OFFSET ?',
                               ([owner] if owner is not None else []) + [*words, offset]).fetchall()

    def offset(self):
        row = self.db.execute("SELECT value FROM state WHERE key='offset'").fetchone()
        return int(row[0]) if row else 0

    def advance(self, offset):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (str(offset),))


HELP = ('Отправьте мне музыку как аудио с плеером Telegram — я добавлю её в вашу библиотеку.\n'
        'Затем в любом чате напишите @{username} название или исполнителя.\n'
        'Пустой запрос покажет последние треки. Подпись к аудио тоже участвует в поиске.\n\n'
        '/library — последние 20 треков')


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
    if 'audio' in message:
        title = library.save(owner, message['audio'], message.get('caption', ''))
        row = library.db.execute('SELECT * FROM tracks WHERE owner=? AND unique_id=?',
                                 (owner, message['audio']['file_unique_id'])).fetchone()
        try:
            library.upload(api, row, chat)
            text = f'Добавлено в общую библиотеку, сохранено в папку и проверено для inline: {title}\nПоиск: @{username} название'
        except (APIError, OSError) as error:
            text = f'Трек добавлен в базу: {title}\nПодготовка не завершена: {error}\nПовторите /repair — локальная копия, если скачана, сохранена.'
    elif message.get('text', '').split('@')[0].split(' ')[0] in ('/repair', '/cache'):
        cache_only = message['text'].split('@')[0].split(' ')[0] == '/cache'
        rows = library.db.execute('SELECT * FROM tracks WHERE owner=? ORDER BY id', (owner,)).fetchall()
        api.call('sendMessage', chat_id=chat, text=f'Обрабатываю треки: {len(rows)}. ' +
                 ('Скачиваю в папку music.' if cache_only else 'Загружаю из папки music; пришлю аудио обратно.'))
        repaired = 0
        for row in rows:
            try:
                if cache_only:
                    library.cache(api, row)
                else:
                    library.upload(api, row, chat)
                repaired += 1
            except (APIError, OSError) as error:
                api.call('sendMessage', chat_id=chat, text=f'Не удалось восстановить {row["title"]}: {error}')
            time.sleep(1)
        text = f'Обновлено {repaired} из {len(rows)}. Повторите поиск: @{username} '
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
    LOG.info('Бот @%s запущен (версия 1.7, общая библиотека). Музыка: %s. Для остановки нажмите Ctrl+C.', me['username'], library.music_dir)
    if not me.get('supports_inline_queries'):
        LOG.warning('Включите inline в @BotFather: /setinline → @%s → Найти музыку', me['username'])
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


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except (APIError, ValueError) as error:
        print(error)
        raise SystemExit(1)
