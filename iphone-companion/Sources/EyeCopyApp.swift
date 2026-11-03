import SwiftUI

@main
struct EyeCopyApp: App {
    var body: some Scene {
        WindowGroup { DocumentsView() }
    }
}

struct DocumentsView: View {
    @State private var files: [URL] = []
    private var documents: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
    }
    var body: some View {
        NavigationStack {
            List {
                Section {
                    Label("On My iPhone › EyeCopy", systemImage: "folder.fill")
                    Text("Connect to EyeCopy on Windows to upload and download these original files. This folder is also available in the Files app.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Section("Documents") {
                    if files.isEmpty {
                        Text("Your folder is ready. Upload a file from EyeCopy on Windows.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(files, id: \.path) { file in
                        Label(file.lastPathComponent, systemImage: file.hasDirectoryPath ? "folder" : "doc")
                    }
                }
            }
            .navigationTitle("EyeCopy")
            .tint(.orange)
            .toolbar { Button(action: refresh) { Label("Refresh", systemImage: "arrow.clockwise") } }
            .refreshable { refresh() }
            .onAppear(perform: refresh)
        }
    }
    private func refresh() {
        files = ((try? FileManager.default.contentsOfDirectory(
            at: documents, includingPropertiesForKeys: [.isDirectoryKey], options: [.skipsHiddenFiles])) ?? [])
            .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
    }
}
