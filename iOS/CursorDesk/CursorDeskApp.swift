import SwiftUI

@main
struct CursorDeskApp: App {
    @StateObject private var model = DeskModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(model)
                .preferredColorScheme(.dark)
        }
    }
}
