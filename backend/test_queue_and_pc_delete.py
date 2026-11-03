"""Queue and selected-copy regressions using isolated project fixtures."""
import contextlib
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

from backend.engine import Engine, UserError, device_key, public_error
from backend.recycle_undo import RecycleUndoHistory
from backend.test_backend import FakeAFC


ROOT = Path(__file__).resolve().parents[1]


class QueueAndPCDeleteTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / 'test-output' / 'backend-unit'
        base.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.temporary.name).resolve()
        self.assertIn(ROOT, self.root.parents)
        self.afc = FakeAFC()
        self.phone = device_key('synthetic-queue-phone')

        @contextlib.contextmanager
        def connector(*args):
            yield self.afc

        self.engine = Engine(self.root / 'data', start=False, connector=connector,
                             download_dir=self.root / 'Videos' / 'EyeCopy')
        self.engine._devices[self.phone] = {'id': self.phone, 'name': 'Fixture Phone', 'online': True}
        self.engine.selected_device = self.phone
        self.engine.current_scope = self.engine.scope(self.phone, 'photos')
        self.engine._catalogues[self.engine.current_scope] = []
        self.engine.state()

    def tearDown(self):
        self.engine.close()
        self.assertIn(ROOT, self.root.parents)
        self.temporary.cleanup()

    def item(self, name='original.JPG', contents=b'synthetic original bytes'):
        path = '/DCIM/100APPLE/' + name
        self.afc.add(path, contents)
        item = self.engine._catalogue_item(self.phone, 'photos', None, path, self.afc.stat(path))
        self.engine._catalogues[self.engine.current_scope].append(item)
        return item

    def download(self, item, destination=None, sync=True):
        result = self.engine.enqueue_download(self.phone, [{'id': item['id']}], destination, sync=sync)
        jobs = [j for j in self.engine._jobs if j['id'] in result['jobIds']]
        for job in jobs:
            self.engine.run_job(job)
            self.assertEqual(job['state'], 'completed')
        return jobs

    def record_fixture(self, item):
        root = Path(self.engine._devices[self.phone]['defaultDownloadDir'])
        root.mkdir(parents=True, exist_ok=True)
        path = root / item['name']
        path.write_bytes(bytes(self.afc.files[item['path']]))
        self.engine._download_records[str(path)] = {
            'deviceId': self.phone, 'itemId': item['id'], 'path': str(path),
            'identity': self.engine._file_identity(path), 'modifiedNs': path.stat().st_mtime_ns,
            'sourceSignature': {'size': item['size'], 'modified': item['modified']},
        }
        return path

    def test_large_bulk_selection_reconciles_once_skips_owned_and_queues_only_new_or_missing(self):
        downloaded = [self.item('owned-%04d.JPG' % index) for index in range(400)]
        paths = [self.record_fixture(item) for item in downloaded]
        paths[-1].unlink()
        new = [self.item('new-%d.JPG' % index) for index in range(2)]
        with mock.patch.object(self.engine, '_validated_pc_records',
                               wraps=self.engine._validated_pc_records) as reconcile, \
                mock.patch.object(self.engine, '_resolve_item', side_effect=AssertionError('No per-item full-library lookup')), \
                mock.patch.object(self.engine, '_save_locked', wraps=self.engine._save_locked) as save:
            result = self.engine.enqueue_download(self.phone, downloaded + new, sync=True)
        self.assertEqual(reconcile.call_count, 1)
        self.assertLessEqual(save.call_count, 2)
        self.assertEqual(result['skipped'], 399)
        self.assertEqual(result['queued'], 3)
        self.assertEqual({j['item']['id'] for j in self.engine._jobs}, {downloaded[-1]['id'], *[i['id'] for i in new]})
        self.assertEqual(self.afc.read_calls, [])

    def test_client_local_path_does_not_authorize_skip_and_external_drops_keep_duplicates(self):
        item = self.item()
        forged = {**item, 'localPath': str(self.root / 'untracked.JPG')}
        result = self.engine.enqueue_download(self.phone, [forged], sync=True)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(result['skipped'], 0)
        self.engine.run_job(self.engine._jobs[-1])
        self.assertEqual(self.engine.enqueue_download(self.phone, [item], sync=True)['skipped'], 1)
        external = str(self.root / 'targeted-copy')
        self.assertEqual(self.engine.enqueue_download(self.phone, [item], external, sync=False)['queued'], 1)
        self.assertEqual(self.engine.enqueue_download(self.phone, [item], external, sync=False)['queued'], 1)

    def test_concurrent_bulk_enqueues_cannot_duplicate_default_sync_jobs(self):
        item = self.item()
        resolve = self.engine.resolve_transfer_items
        barrier = threading.Barrier(2)
        results, errors = [], []

        def synchronized(*args, **kwargs):
            result = resolve(*args, **kwargs)
            barrier.wait(5)
            return result

        def enqueue():
            try:
                results.append(self.engine.enqueue_download(self.phone, [item], sync=True))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(self.engine, 'resolve_transfer_items', side_effect=synchronized):
            threads = [threading.Thread(target=enqueue) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(7)
                self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(self.engine._jobs), 1)
        self.assertEqual(sorted(result['queued'] for result in results), [0, 1])
        self.assertEqual(sum(len(result['inProgressJobIds']) for result in results), 1)

    def test_stop_or_clear_revokes_bulk_preparation_before_it_can_enqueue(self):
        item = self.item()
        resolve = self.engine.resolve_transfer_items
        for command in ('cancel_all_jobs', 'clear_all_jobs'):
            with self.subTest(command=command):
                entered, release = threading.Event(), threading.Event()
                results = []

                def blocked(*args, **kwargs):
                    result = resolve(*args, **kwargs)
                    entered.set()
                    self.assertTrue(release.wait(5))
                    return result

                with mock.patch.object(self.engine, 'resolve_transfer_items', side_effect=blocked):
                    worker = threading.Thread(target=lambda: results.append(self.engine.enqueue_download(self.phone, [item])))
                    worker.start()
                    self.assertTrue(entered.wait(5))
                    self.engine.handle(command)
                    release.set()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(results[0]['cancelled'])
                self.assertEqual(results[0]['queued'], 0)
                self.assertEqual(self.engine._jobs, [])

    def test_slow_pc_stat_does_not_hold_state_lock_or_block_stop_all(self):
        item = self.item()
        self.record_fixture(item)
        verify = self.engine._validated_pc_records
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def slow_stat(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return verify(*args)

        def enqueue():
            try:
                results.append(self.engine.enqueue_download(self.phone, [item]))
            except Exception as error:
                errors.append(error)

        with mock.patch.object(self.engine, '_validated_pc_records', side_effect=slow_stat):
            worker = threading.Thread(target=enqueue)
            worker.start()
            self.assertTrue(entered.wait(5))
            stopped = threading.Event()
            stopper = threading.Thread(target=lambda: (self.engine.cancel_all_jobs(), stopped.set()))
            stopper.start()
            try:
                self.assertTrue(stopped.wait(1), 'Stop All must respond before slow filesystem verification resumes')
            finally:
                release.set()
                worker.join(5)
                stopper.join(5)
        self.assertEqual(errors, [])
        self.assertTrue(results[0]['cancelled'])
        self.assertEqual(self.engine._jobs, [])

    def test_json_reader_cancels_blocked_setup_and_revokes_pool_queued_requests_with_trusted_epochs(self):
        item = self.item()
        self.record_fixture(item)
        self.engine.save()
        entered, release, count = [self.root / name for name in ('protocol-entered', 'protocol-release', 'protocol-count')]
        harness = self.root / 'protocol_fixture.py'
        harness.write_text(
            'import sys, threading, time\n'
            'from pathlib import Path\n'
            f'sys.path.insert(0, {str(ROOT)!r})\n'
            'from backend import engine as subject\n'
            'original_init = subject.Engine.__init__\n'
            'def initialize(self, *args, **kwargs):\n'
            '    kwargs["start"] = False\n'
            '    original_init(self, *args, **kwargs)\n'
            'subject.Engine.__init__ = initialize\n'
            'original_verify = subject.Engine._validated_pc_records\n'
            'def slow_verify(self, *args):\n'
            '    if threading.current_thread().name.startswith("iphone-queue"):\n'
            f'        marker, release, counter = Path({str(entered)!r}), Path({str(release)!r}), Path({str(count)!r})\n'
            '        counter.write_text(str(int(counter.read_text()) + 1) if counter.exists() else "1")\n'
            '        marker.write_text("waiting")\n'
            '        deadline = time.monotonic() + 15\n'
            '        while not release.exists():\n'
            '            if time.monotonic() > deadline: raise TimeoutError("Protocol fixture was never released")\n'
            '            time.sleep(0.01)\n'
            '    return original_verify(self, *args)\n'
            'subject.Engine._validated_pc_records = slow_verify\n'
            'subject.main()\n', encoding='utf-8')
        env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'TEMP': str(self.root), 'TMP': str(self.root)}
        process = subprocess.Popen([sys.executable, '-B', str(harness), '--data-dir', str(self.engine.data_dir),
                                    '--download-dir', str(self.engine.download_root)], cwd=ROOT, env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        messages, buffered = queue.Queue(), {}

        def read_output():
            for line in process.stdout:
                messages.put(json.loads(line))

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        def send(identifier, command, args=None):
            process.stdin.write(json.dumps({'id': identifier, 'command': command, 'args': args or {}}) + '\n')
            process.stdin.flush()

        def receive(identifier, timeout=5):
            deadline = time.monotonic() + timeout
            while identifier not in buffered:
                message = messages.get(timeout=max(0.001, deadline - time.monotonic()))
                if message.get('id') is not None:
                    buffered[message['id']] = message
            return buffered.pop(identifier)

        args = {'deviceId': self.phone, 'items': [{'id': item['id']}], 'enqueue_generation': 1}
        try:
            send('running', 'download', args)
            deadline = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(entered.exists(), 'The real JSON-lines dispatcher must start the queued worker')
            for identifier in ('queued-one', 'queued-two', 'queued-three', 'overflow'):
                send(identifier, 'download', args)
            overflow = receive('overflow')
            self.assertIn('selection is still being prepared', overflow['error'])
            send('stop', 'cancel_all_jobs')
            self.assertIn('result', receive('stop', timeout=2))
            self.assertFalse(release.exists(), 'Stop returned while disk verification was still blocked')
            release.write_text('continue')
            for identifier in ('running', 'queued-one', 'queued-two', 'queued-three'):
                response = receive(identifier)
                self.assertTrue(response['result']['cancelled'], response)
                self.assertEqual(response['result']['queued'], 0)
            self.assertEqual(count.read_text(), '1', 'Pre-stop pool requests must be rejected before starting setup')
            send('state', 'state')
            self.assertEqual(receive('state')['result']['jobs'], [])
        finally:
            release.write_text('continue')
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(3)
            reader.join(2)
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
        self.assertEqual(process.returncode, 0, stderr)

    def test_stop_all_signals_active_once_and_cancels_idle_states_without_waiting_on_usb(self):
        for state in ('queued', 'paused', 'waiting', 'transferring', 'completed'):
            job = self.engine._new_job(state, 'download', self.phone, 10, state=state)
            if state == 'paused':
                job['awaitingStart'] = True
            self.engine._jobs.append(job)
        active = self.engine._jobs[-2]
        self.engine._active_job = active['id']
        with mock.patch.object(self.engine, '_afc', side_effect=AssertionError('Stop must not wait on a phone connection')), \
                mock.patch.object(self.engine, '_save_locked', wraps=self.engine._save_locked) as save:
            result = self.engine.handle('cancel_all_jobs')
        self.assertEqual(result['count'], 4)
        self.assertEqual(save.call_count, 1)
        self.assertTrue(active['cancelRequested'])
        self.assertEqual(active['message'], 'Stopping transfer…')
        self.assertTrue(next(j for j in result['jobs'] if j['id'] == active['id'])['cancelRequested'])
        self.assertTrue(all(j['state'] == 'cancelled' for j in self.engine._jobs[:3]))
        self.assertEqual(self.engine._jobs[-1]['state'], 'completed')
        self.assertFalse(self.engine._jobs[1]['awaitingStart'])
        self.assertEqual(self.engine.handle('cancel_all_jobs')['count'], 0)

    def test_cancel_wins_a_transport_timeout_and_stays_cancelled_instead_of_retrying(self):
        item = self.item()
        self.engine.enqueue_download(self.phone, [item])
        job = self.engine._jobs[-1]
        entered, release = threading.Event(), threading.Event()
        self.engine._active_job = job['id']

        def blocked_transfer(value):
            entered.set()
            self.assertTrue(release.wait(5))
            raise TimeoutError('synthetic USB timeout after cancel')

        with mock.patch.object(self.engine, '_transfer_download', side_effect=blocked_transfer), \
                self.assertLogs('eyecopy', level='WARNING'):
            thread = threading.Thread(target=lambda: self.engine.run_job(job))
            thread.start()
            self.assertTrue(entered.wait(5))
            self.engine.cancel_all_jobs()
            release.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(job['state'], 'cancelled')
        self.assertNotIn('cancelRequested', job)
        self.assertIsNone(self.engine._active_job)
        self.assertIsNone(self.engine._next_work())
        self.assertTrue(self.engine._devices[self.phone]['online'])

    def test_clear_all_removes_idle_listings_and_owned_parts_but_keeps_completed_originals(self):
        item = self.item()
        completed = self.download(item)[0]
        original = Path(completed['localPath'])
        for state in ('queued', 'paused', 'waiting', 'failed', 'cancelled'):
            job = self.engine._new_job(state, 'download', self.phone, 20, state=state,
                                       destination=str(self.root / 'parts'), awaitingStart=True)
            Path(job['destination']).mkdir(exist_ok=True)
            part = Path(job['destination']) / ('original.eyecopy-' + job['id'] + '.part')
            part.write_bytes(b'owned partial')
            job['partPath'] = str(part)
            self.engine._jobs.append(job)
        unrelated = self.root / 'parts' / 'unrelated.part'
        unrelated.write_bytes(b'keep untracked bytes')
        result = self.engine.handle('clear_all_jobs')
        self.assertEqual(result['cleared'], 6)
        self.assertEqual(result['retained'], 0)
        self.assertEqual(self.engine._jobs, [])
        self.assertTrue(original.is_file())
        self.assertEqual(unrelated.read_bytes(), b'keep untracked bytes')
        self.assertEqual(self.engine.preview_pc_cleanup(self.phone)['fileCount'], 1)

    def test_clear_all_keeps_active_upload_cleanup_and_locked_or_unowned_partial_records(self):
        directory = self.root / 'parts'
        directory.mkdir()
        active = self.engine._new_job('active', 'download', self.phone, 20, state='transferring')
        self.engine._active_job = active['id']
        upload = self.engine._new_job('upload', 'upload', self.phone, 20, state='cancelled', remoteCleanupPending=True)
        locked = self.engine._new_job('locked', 'download', self.phone, 20, state='failed', destination=str(directory))
        locked_path = directory / ('locked.eyecopy-' + locked['id'] + '.part')
        locked_path.write_bytes(b'owned but locked')
        locked['partPath'] = str(locked_path)
        unowned = self.engine._new_job('unowned', 'download', self.phone, 20, state='failed', destination=str(directory))
        unowned_path = self.root / ('outside.eyecopy-' + unowned['id'] + '.part')
        unowned_path.write_bytes(b'keep unrelated path')
        unowned['partPath'] = str(unowned_path)
        self.engine._jobs = [active, upload, locked, unowned]
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == locked_path:
                raise PermissionError('synthetic sharing lock')
            return original_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, 'unlink', unlink):
            result = self.engine.clear_all_jobs()
        self.assertEqual(result['cleared'], 0)
        self.assertEqual(result['retained'], 4)
        self.assertEqual(len(result['blocked']), 4)
        self.assertTrue(locked_path.exists())
        self.assertTrue(unowned_path.exists())
        self.assertTrue(active['cancelRequested'])

    def test_selected_pc_delete_plan_ignores_external_edited_missing_and_unselected_files(self):
        items = [self.item(name) for name in ('selected.JPG', 'edited.JPG', 'missing.JPG', 'external.JPG', 'unselected.JPG')]
        jobs = [self.download(item, str(self.root / 'external') if item['name'] == 'external.JPG' else None)[0] for item in items]
        Path(jobs[1]['localPath']).write_bytes(b'edited original copy')
        Path(jobs[2]['localPath']).unlink()
        supplied = [{'id': i['id'], 'localPath': jobs[3]['localPath']} for i in items[:4]]
        plan = self.engine.handle('preview_pc_delete', {'deviceId': self.phone, 'items': supplied})
        self.assertEqual(plan['fileCount'], 1)
        self.assertEqual(plan['files'][0]['path'], jobs[0]['localPath'])
        self.assertIn('unselected PC files', plan['warning'])
        with mock.patch.object(self.engine, '_remove_pc_copy', side_effect=lambda path, validate: path.unlink() if validate() else None):
            result = self.engine.handle('confirm_pc_delete', {'token': plan['token']})
        self.assertEqual(result['deleted'], 1)
        self.assertTrue(Path(jobs[1]['localPath']).exists())
        self.assertTrue(Path(jobs[3]['localPath']).exists())
        self.assertTrue(Path(jobs[4]['localPath']).exists())
        self.assertTrue(all(self.afc.exists(item['path']) for item in items))

    def test_selected_pc_confirmation_rechecks_identity_and_uses_separate_token_kind(self):
        item = self.item()
        job = self.download(item)[0]
        plan = self.engine.preview_pc_delete(self.phone, [item])
        with self.assertRaises(UserError):
            self.engine.confirm_pc_cleanup(plan['token'])
        plan = self.engine.preview_pc_delete(self.phone, [item])
        Path(job['localPath']).write_bytes(b'new edited bytes')
        with mock.patch.object(self.engine, '_remove_pc_copy', side_effect=AssertionError('Edited copy must be kept')):
            result = self.engine.confirm_pc_delete(plan['token'])
        self.assertEqual(result['failed'], 1)
        self.assertTrue(Path(job['localPath']).exists())

    def install_private_recycler(self):
        """Use receipt validation/history, but move bytes only inside this fixture."""
        private = self.root / 'private-recycle'
        private.mkdir()
        saved = {}

        def identity(path):
            stat = Path(path).stat()
            return {'volume': stat.st_dev, 'fileId': stat.st_ino, 'size': stat.st_size, 'modified': stat.st_mtime_ns}

        def recycle(path, validate):
            self.assertIn(self.root, Path(path).resolve().parents)
            self.assertTrue(validate())
            identifier = uuid.uuid4().hex
            original_identity = identity(path)
            owned = private / identifier
            Path(path).rename(owned)
            saved[identifier] = owned
            bin_path = Path('C:/') / '$Recycle.Bin' / 'S-1-5-21-1000' / ('$R' + identifier)
            return {'version': 1, 'id': identifier, 'originalPath': str(path),
                    'binPath': str(bin_path), 'infoPath': str(bin_path.with_name('$I' + identifier)),
                    'originalIdentity': original_identity, 'binIdentity': original_identity,
                    'parentIdentity': identity(Path(path).parent),
                    'infoIdentity': {'volume': 1, 'fileId': 123, 'size': 80, 'modified': 1},
                    'infoDigest': '0' * 64}

        def restore(receipt, validate):
            self.assertTrue(validate())
            target = Path(receipt['originalPath'])
            self.assertIn(self.root, target.resolve().parents)
            if target.exists():
                raise FileExistsError('Keep the newly occupying file')
            saved[receipt['id']].rename(target)
            return {'restoredIdentity': identity(target), 'infoCleanupPending': False}

        self.engine._pc_history = RecycleUndoHistory(self.engine.data_dir, recycler=recycle,
            restorer=restore, identity=identity, cleanup=lambda receipt: True)
        return recycle

    def test_selected_live_photo_deletion_recycles_both_originals_and_undo_redo_restores_only_them(self):
        photo, motion, untouched = self.item('pair.JPG'), self.item('pair.MOV'), self.item('untouched.JPG')
        photo['livePhoto'] = {'verified': True, 'video': copy.deepcopy(motion), 'totalSize': photo['size'] + motion['size']}
        self.download(photo)
        untouched_job = self.download(untouched)[0]
        plan = self.engine.preview_pc_delete(self.phone, [photo, motion])
        self.assertEqual(plan['fileCount'], 2)
        recycle = self.install_private_recycler()
        with mock.patch.object(self.engine, '_remove_pc_copy', side_effect=recycle):
            result = self.engine.confirm_pc_delete(plan['token'])
        self.assertEqual(result['deleted'], 2)
        self.assertTrue(result['undoToken'])
        self.assertTrue(Path(untouched_job['localPath']).exists())
        restored = self.engine.handle('undo_pc_cleanup', {'token': result['undoToken']})
        self.assertEqual(restored['restored'], 2)
        catalogue = self.engine.state()['catalogue']
        pair = next(i for i in catalogue if i['id'] == photo['id'])
        self.assertTrue(Path(pair['localPath']).exists())
        self.assertTrue(Path(pair['livePhoto']['video']['localPath']).exists())
        redone = self.engine.handle('redo_pc_cleanup', {'token': restored['redoToken']})
        self.assertEqual(redone['deleted'], 2)
        self.assertTrue(Path(untouched_job['localPath']).exists())
        self.assertTrue(all(self.afc.exists(i['path']) for i in (photo, motion, untouched)))

    def test_compatible_local_preview_seek_forwards_guarded_original_time_without_phone_reads(self):
        item = self.item('clip.MOV')
        job = self.download(item)[0]
        with mock.patch('backend.streaming.shutil.which', return_value='fixture-ffmpeg.exe'), \
                mock.patch.object(self.engine, '_afc', side_effect=AssertionError('Local compatible preview needs no phone')):
            result = self.engine.handle('preview', {'item': {'id': item['id']}, 'compatible': True, 'start': 41.25})
            self.assertEqual(result['start'], 41.25)
            self.assertEqual(result['mode'], 'compatible')
            self.assertTrue(result['downloaded'])
            session = next(iter(self.engine._media.sessions.values()))
            self.assertEqual(session['local'], Path(job['localPath']))
            for value in (-1, float('nan'), float('inf'), 86401, 'invalid'):
                with self.subTest(start=value), self.assertRaises(UserError):
                    self.engine.handle('preview', {'item': {'id': item['id']}, 'compatible': True, 'start': value})

    def test_generic_device_copy_preserves_saved_names_protocol_ids_and_platform_locations(self):
        self.assertRegex(self.phone, r'^iphone-[0-9a-f]{20}$')
        self.assertIn('Device', self.engine.connection['message'])
        self.assertNotIn('iPhone', self.engine.connection['message'])
        for error in (ConnectionResetError(), type('NotPairedError', (Exception,), {})(),
                      type('PasswordProtectedError', (Exception,), {})()):
            with self.subTest(error=type(error).__name__):
                message = public_error(error)
                self.assertIn('Device', message)
                self.assertNotIn('iPhone', message)
        self.engine._devices[self.phone]['name'] = 'My iPhone name stays exact'
        self.assertEqual(self.engine.state()['devices'][0]['name'], 'My iPhone name stays exact')
        self.engine._apps[self.phone] = []
        self.assertIn('On My iPhone', self.engine.files_home(self.phone)['message'])
        self.assertIn('Device originals', self.engine.preview_pc_cleanup(self.phone)['warning'])


if __name__ == '__main__':
    unittest.main()
