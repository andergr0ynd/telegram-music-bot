import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from bot import APIError, Library, handle


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))

    def cache_audio(self, file_id, destination):
        destination = Path(destination)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b'fake audio')
        return destination

    def upload_audio(self, chat, path, title, performer):
        return {'file_id': 'reuploaded'}


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(':memory:', self.temp.name)

    def tearDown(self):
        self.library.db.close()
        self.temp.cleanup()

    def audio(self, uid='one', title='Песня', file_id='file'):
        return dict(file_unique_id=uid, file_id=file_id, title=title, performer='Артист')

    def test_search_and_privacy(self):
        self.library.save(1, self.audio(), 'Любимое')
        self.assertEqual(len(self.library.search(1, 'ПЕСНЯ артист любимое')), 1)
        self.assertEqual(len(self.library.search(2, '')), 0)
        self.assertEqual(len(self.library.search(1, 'другое')), 0)

    def test_duplicate_updates_file(self):
        self.library.save(1, self.audio())
        self.library.save(1, self.audio(file_id='new'))
        rows = self.library.search(1, '')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['file_id'], 'new')

    def test_inline_pagination(self):
        for i in range(52):
            self.library.save(1, self.audio(uid=str(i)))
        api = FakeAPI()
        query = dict(id='q', query='', offset='', **{'from': {'id': 1}})
        handle({'inline_query': query}, api, self.library, 'test_bot')
        method, result = api.calls[-1]
        self.assertEqual(method, 'answerInlineQuery')
        self.assertEqual(len(result['results']), 50)
        self.assertEqual(result['next_offset'], '50')
        self.assertTrue(result['is_personal'])
        self.assertEqual(result['results'][0]['audio_file_id'], 'file')
        self.assertEqual(result['results'][0]['title'], 'Песня')
        query['offset'] = '50'
        handle({'inline_query': query}, api, self.library, 'test_bot')
        self.assertEqual(len(api.calls[-1][1]['results']), 2)
        self.assertEqual(api.calls[-1][1]['next_offset'], '')

    def test_private_upload_and_group_ignored(self):
        api = FakeAPI()
        msg = {'chat': {'id': 1, 'type': 'private'}, 'from': {'id': 1}, 'audio': self.audio()}
        handle({'message': msg}, api, self.library, 'test_bot')
        self.assertEqual(len(self.library.search(1, '')), 1)
        self.assertIn('сохранено в папку', api.calls[-1][1]['text'])
        msg['chat']['type'] = 'group'
        msg['audio'] = self.audio(uid='second')
        handle({'message': msg}, api, self.library, 'test_bot')
        self.assertEqual(len(self.library.search(1, '')), 1)

    def test_persistent_offset(self):
        self.assertEqual(self.library.offset(), 0)
        self.library.advance(123)
        self.assertEqual(self.library.offset(), 123)

    def test_inline_title_fallback_for_existing_record(self):
        self.library.save(1, self.audio(title='   '))
        self.assertEqual(self.library.search(1, '')[0]['title'], 'Без названия')
        self.library.db.execute("UPDATE tracks SET title='   '")
        api = FakeAPI()
        handle({'inline_query': {'id': 'q', 'from': {'id': 1}, 'query': ''}},
               api, self.library, 'test_bot')
        self.assertEqual(api.calls[-1][1]['results'][0]['title'], 'Без названия')

    def test_telegram_rejects_cached_audio_title(self):
        self.library.save(1, self.audio())

        class RejectAudio(FakeAPI):
            def call(self, method, **payload):
                super().call(method, **payload)
                if method == 'savePreparedInlineMessage' or payload['results'][0]['type'] == 'audio':
                    raise APIError(400, 'Bad Request: AUDIO_TITLE_EMPTY')

        api = RejectAudio()
        handle({'inline_query': {'id': 'q', 'from': {'id': 1}, 'query': ''}},
               api, self.library, 'test_bot')
        self.assertEqual(len(api.calls), 3)
        result = api.calls[-1][1]['results'][0]
        self.assertEqual(result['type'], 'article')
        self.assertIn('/repair', result['input_message_content']['message_text'])
        self.assertEqual(result['title'], 'Песня')

    def test_bad_track_does_not_hide_good_audio(self):
        self.library.save(1, self.audio(uid='good', file_id='good'))
        self.library.save(1, self.audio(uid='bad', file_id='bad'))

        class MixedAPI(FakeAPI):
            def call(self, method, **payload):
                super().call(method, **payload)
                results = [payload['result']] if method == 'savePreparedInlineMessage' else payload['results']
                if any(r.get('audio_file_id') == 'bad' for r in results):
                    raise APIError(400, 'AUDIO_TITLE_EMPTY')

        api = MixedAPI()
        handle({'inline_query': {'id': 'q', 'from': {'id': 1}, 'query': ''}},
               api, self.library, 'test_bot')
        self.assertEqual([r['audio_file_id'] for r in api.calls[-1][1]['results']], ['good'])

    def test_repair_updates_only_owner_and_keeps_search(self):
        self.library.save(1, self.audio(), 'Метка')
        self.library.save(2, self.audio())

        class RepairAPI(FakeAPI):
            def upload_audio(self, chat, path, title, performer):
                return {'file_id': 'reuploaded'}

        api = RepairAPI()
        with patch('bot.time.sleep'):
            handle({'message': {'chat': {'id': 1, 'type': 'private'},
                                'from': {'id': 1}, 'text': '/repair'}},
                   api, self.library, 'test_bot')
        self.assertEqual(self.library.search(1, 'метка')[0]['file_id'], 'reuploaded')
        self.assertEqual(self.library.search(2, '')[0]['file_id'], 'file')

    def test_local_copy_survives_reopen_and_is_reused(self):
        self.library.save(1, self.audio())
        row = self.library.search(1, '')[0]
        api = FakeAPI()
        saved = self.library.cache(api, row)
        self.assertEqual(saved.read_bytes(), b'fake audio')
        self.assertEqual(self.library.search(1, '')[0]['local_path'], str(saved.resolve()))
        class OfflineCache(FakeAPI):
            def cache_audio(self, file_id, destination):
                if Path(destination).is_file():
                    return Path(destination)
                raise APIError(0, 'offline')
        self.library.upload(OfflineCache(), row, 1)
        self.assertEqual(self.library.search(1, '')[0]['file_id'], 'reuploaded')

    def test_cache_command_does_not_upload(self):
        self.library.save(1, self.audio())
        class CacheOnly(FakeAPI):
            def upload_audio(self, *args):
                raise AssertionError('cache must not upload')
        with patch('bot.time.sleep'):
            handle({'message': {'chat': {'id': 1, 'type': 'private'}, 'from': {'id': 1}, 'text': '/cache'}},
                   CacheOnly(), self.library, 'test_bot')
        self.assertTrue(Path(self.library.search(1, '')[0]['local_path']).is_file())

    def test_other_user_can_find_shared_music_inline(self):
        self.library.save(1, self.audio(), 'вечер')
        api = FakeAPI()
        handle({'inline_query': {'id': 'q', 'from': {'id': 999}, 'query': 'ПЕСНЯ вечер'}},
               api, self.library, 'test_bot')
        self.assertEqual(len(api.calls[-1][1]['results']), 1)
        handle({'inline_query': {'id': 'q2', 'from': {'id': 999}, 'query': 'нет совпадений'}},
               api, self.library, 'test_bot')
        self.assertEqual(api.calls[-1][1]['results'], [])

    def test_shared_results_deduplicate_and_search_in_private_chat(self):
        self.library.save(1, self.audio())
        self.library.save(2, self.audio())
        self.assertEqual(len(self.library.search(None, 'песня')), 1)
        api = FakeAPI()
        handle({'message': {'chat': {'id': 999, 'type': 'private'}, 'from': {'id': 999},
                            'text': '/search ПЕСНЯ'}}, api, self.library, 'test_bot')
        self.assertIn('Песня', api.calls[-1][1]['text'])


if __name__ == '__main__':
    unittest.main()
