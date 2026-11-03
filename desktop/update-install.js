'use strict';

// Installation requires idle transfers and a verified candidate on both sides
// of the native confirmation dialog. Checking never installs automatically.
function createUpdateInstallController({ packaged, service, requireIdle, confirm, ready }) {
  let preparing = false;
  return {
    busy() { return preparing; },
    async install() {
      if (preparing) throw new Error('An update installation is already being prepared.');
      preparing = true;
      try {
        if (!packaged()) throw new Error('Install the Windows package to update this application. Source checkouts use their build workflow.');
        await requireIdle();
        if (service.get().status !== 'ready') await service.download();
        await requireIdle();
        const candidate = await service.installer();
        if (!await confirm(candidate)) return service.get();
        await requireIdle();
        const verified = await service.installer();
        ready(verified);
        return { ...service.get(), status: 'installing', message: 'Closing EyeCopy to open the update installer…' };
      } finally { preparing = false; }
    }
  };
}

const UPDATE_MUTATIONS = new Set(['download', 'queue_download', 'upload', 'create_folder', 'confirm_phone_delete',
  'confirm_phone_folder_delete', 'confirm_pc_cleanup', 'confirm_pc_delete', 'undo_pc_cleanup', 'redo_pc_cleanup',
  'set_default_download_dir', 'set_download_dir', 'reset_download_dir', 'undo_download_location', 'redo_download_location',
  'rename_device', 'clear_cache', 'ensure_files_home', 'configure_files_home', 'retry_job', 'prepare_drag',
  'native_drag', 'pick_upload_files', 'choose_job_folder']);

function requireUpdateOperationAllowed(command, busy) {
  if (busy && UPDATE_MUTATIONS.has(command)) throw new Error('An update is being prepared. Cancel the update or finish installation before starting file operations.');
}

module.exports = { createUpdateInstallController, requireUpdateOperationAllowed, UPDATE_MUTATIONS };
