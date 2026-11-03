# EyeCopy native Explorer drag integration

EyeCopy supports three copy routes. [DragManager](../drag-manager.js) chooses the route from the transfer engine's authoritative catalogue and coordinates preparation, mouse handoff, cancellation and queue reporting.

| Source | Route | Destination and data handling |
| --- | --- | --- |
| Verified original in the phone's current default PC folder | Electron `startDrag` | Explorer copies an existing local file using Windows file-drag behavior. |
| Original on the iPhone, the normal phone route | Native local ticket | Explorer copies a small temporary ticket. EyeCopy verifies that exact ticket in the destination, removes it, and starts an engine download there. |
| Original exposed as a virtual file, an optional internal route | COM virtual-file helper | Explorer requests file contents through `IStream` and writes the destination itself. |

The portable Windows package includes compiled helpers and native bindings. No compiler installation is needed to run that package. Development builds can compile the helper from [VirtualFileDrag.cs](VirtualFileDrag.cs) when the bundled executable is unavailable.

## Catalogue authority and preparation

`resolve_transfer_items` resolves selected identities from the engine catalogue and expands verified Live Photos into their still and motion originals. Renderer-supplied companion paths are not trusted. Each drag supports up to 100 original files, including expanded companions.

Only verified copies in the current permanent default folder can supply catalogue `localPath` values. A completed transfer's queue result may link to an external destination, but it does not promote that copy into the default library. Refresh removes stale badges after deletion or replacement.

Idle preparations are invalidated when engine state changes the relevant default folder or local-copy authority. An unchanged local preparation retains its fast mouse handoff. A missing local original falls back to the phone route when the iPhone is available; otherwise the operation reports that the phone must be connected.

Prepared streams and tickets have bounded lifetimes. Each accepted phone copy owns its preparation independently. Discarding another idle preparation does not cancel an active transfer.

## Native mouse handoff

The trusted source window comes from Electron's `BrowserWindow.fromWebContents(sender).getNativeWindowHandle()`. The main process preserves the full native handle and supplies its own process identity. Renderer code cannot choose an unrelated window or process.

[foreground.js](foreground.js) checks the live input gesture and releases the source's mouse capture before Windows takes over the drag. The left mouse button must remain held. Escape, a lost gesture or a rejected copy produces a terminal result or a responsive destination fallback.

For phone tickets, [local-ticket.js](local-ticket.js) runs on Electron's Windows UI/OLE thread. It opens the ordinary ticket through the native Windows file API, checks its bounded contents and builds the Shell data object. `SHDoDragDrop` owns the drag image, copy cursor and modal drag loop. COM objects and PIDLs are released when their ownership ends.

Only the exact small ticket is exposed during this ordinary Windows drag gesture. The Shell callback never reads the phone original or waits for a video transfer. After the gesture returns, folder verification and engine queueing run asynchronously. Windows owns the nested gesture until release or Escape; transfer controls resume when it ends. Idle preparations and late queue acknowledgements are revoked by Stop all so cancelled work cannot start afterward.

The marker detector verifies the destination by consuming the exact nonce-bearing ticket. A folder observed under the release point is a candidate only; it cannot start a phone transfer by itself. Once verified, EyeCopy records deferred engine jobs and requests their guarded start. Cancellation during this handoff cannot revive or duplicate those jobs. If automatic detection cannot confirm the destination, the queue offers **Choose folder…**.

## Virtual-file helper

[broker.js](broker.js) exports `createDragBroker({ dataDir, onEvent, onError })`:

- `ready()` locates the bundled helper, starts its hidden process and waits for protocol readiness.
- `begin(files, id, { sourceWindow })` submits one copy while its trusted mouse gesture remains active.
- `cancel(id)` revokes that copy's activation permission and requests cancellation.
- `close()` ends the helper and releases outstanding operations.

The private standard-input protocol uses JSON lines with `begin`, `foreground_ack`, `cancel` and `quit` commands. A file descriptor contains `name`, `size`, `url` and `modified`. URLs must be engine-issued HTTP streams on `127.0.0.1`, with a port and an opaque original-file token. Remote hosts, redirects and arbitrary source URLs are rejected. Signed stream URLs are not written to helper logs.

The COM data object provides file descriptors, indexed file-content streams and `IDataObjectAsyncCapability`. Explorer may read after the pointer gesture ends, so accepting a drop does not prove that every file has finished copying. Queue completion follows successful reads and the asynchronous Shell result. Cancellation interrupts pending reads; helper failures and stalled copies terminate their queue entries and release their stream tokens.

## Owned partial files and recovery

Engine downloads persist their selected destination, source signature and exact job-owned temporary path before writing file data. Partial files belong only to that recorded job and directory. The engine never sweeps arbitrary `*.part` files or overwrites an existing destination.

Before publication, the engine checks the original's metadata and the received size, flushes the local file and closes the phone handle. Complete partial copies survive short Windows locks and can be finalized by retry without receiving their bytes again. Publication records the output identity so restart can recognize a completed rename. **Clear finished** retains recoverable partials and active cleanup obligations.

Uploads use an exact job-owned temporary document in the selected app directory. Cancellation removes only that temporary document after its ownership and location are validated. Existing iPhone documents and PC source files are preserved.

Explorer owns files written by the virtual-file route. EyeCopy does not have authoritative ownership of Explorer's partial destination files and does not remove them automatically. Errors instruct the user to inspect the chosen destination before retrying.

For normal operating guidance, see [GUIDE.html](../../GUIDE.html).
