import SwiftUI

struct ContentView: View {
    @EnvironmentObject private var model: DeskModel
    @State private var webURL: URL?
    @State private var showWeb = false

    var body: some View {
        ZStack {
            LinearGradient(
                colors: [
                    Color(red: 0.05, green: 0.07, blue: 0.10),
                    Color(red: 0.09, green: 0.14, blue: 0.12),
                    Color(red: 0.04, green: 0.05, blue: 0.07)
                ],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
            .ignoresSafeArea()

            // subtle grid atmosphere
            GeometryReader { geo in
                Path { path in
                    let step: CGFloat = 28
                    for x in stride(from: 0, through: geo.size.width, by: step) {
                        path.move(to: CGPoint(x: x, y: 0))
                        path.addLine(to: CGPoint(x: x, y: geo.size.height))
                    }
                    for y in stride(from: 0, through: geo.size.height, by: step) {
                        path.move(to: CGPoint(x: 0, y: y))
                        path.addLine(to: CGPoint(x: geo.size.width, y: y))
                    }
                }
                .stroke(Color.white.opacity(0.035), lineWidth: 1)
            }
            .ignoresSafeArea()

            VStack(alignment: .leading, spacing: 28) {
                HStack {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("CURSORDESK")
                            .font(.custom("Menlo-Bold", size: 14))
                            .tracking(4)
                            .foregroundStyle(Color(red: 0.45, green: 0.95, blue: 0.72))
                        Text(model.projectLabel)
                            .font(.custom("AvenirNext-Bold", size: 28))
                            .foregroundStyle(.white)
                            .lineLimit(2)
                    }
                    Spacer()
                    Button {
                        model.showSettings = true
                    } label: {
                        Image(systemName: "slider.horizontal.3")
                            .font(.system(size: 18, weight: .semibold))
                            .foregroundStyle(.white.opacity(0.85))
                            .padding(12)
                            .background(Circle().fill(.white.opacity(0.08)))
                    }
                    .accessibilityLabel("Settings")
                }

                Text("One tap into your PC’s Cursor session — local MCP stays on the machine.")
                    .font(.custom("AvenirNext-Regular", size: 16))
                    .foregroundStyle(.white.opacity(0.72))
                    .fixedSize(horizontal: false, vertical: true)

                Spacer(minLength: 12)

                Button(action: connectMoonlight) {
                    HStack(spacing: 14) {
                        Image(systemName: "desktopcomputer")
                            .font(.system(size: 22, weight: .bold))
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Enter Cursor")
                                .font(.custom("AvenirNext-Bold", size: 22))
                            Text(model.hostAddress.isEmpty ? "Set host IP in settings" : model.hostAddress)
                                .font(.custom("Menlo-Regular", size: 12))
                                .opacity(0.8)
                        }
                        Spacer()
                        Image(systemName: "arrow.up.right")
                            .font(.system(size: 18, weight: .bold))
                    }
                    .foregroundStyle(Color(red: 0.04, green: 0.08, blue: 0.06))
                    .padding(.horizontal, 22)
                    .padding(.vertical, 22)
                    .background(
                        RoundedRectangle(cornerRadius: 18, style: .continuous)
                            .fill(Color(red: 0.45, green: 0.95, blue: 0.72))
                    )
                }
                .disabled(model.hostAddress.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                Button(action: openSunshineWeb) {
                    Label("Open Sunshine pair page", systemImage: "link")
                        .font(.custom("AvenirNext-Medium", size: 16))
                        .foregroundStyle(.white.opacity(0.9))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                        .background(
                            RoundedRectangle(cornerRadius: 14, style: .continuous)
                                .stroke(.white.opacity(0.18), lineWidth: 1)
                        )
                }

                Text(model.status)
                    .font(.custom("Menlo-Regular", size: 12))
                    .foregroundStyle(.white.opacity(0.45))

                Spacer()

                Text("True Unreal MCP only works inside Cursor on the PC. This app gets you onto that desktop fast — it is not a cloud agent.")
                    .font(.custom("AvenirNext-Regular", size: 13))
                    .foregroundStyle(.white.opacity(0.4))
            }
            .padding(24)
        }
        .sheet(isPresented: $model.showSettings) {
            SettingsView()
                .environmentObject(model)
        }
        .sheet(isPresented: $showWeb) {
            if let webURL {
                NavigationStack {
                    DeskWebView(url: webURL)
                        .navigationTitle("Sunshine")
                        .navigationBarTitleDisplayMode(.inline)
                        .toolbar {
                            ToolbarItem(placement: .topBarTrailing) {
                                Button("Done") { showWeb = false }
                            }
                        }
                }
            }
        }
    }

    private func connectMoonlight() {
        guard let url = model.connectURL() else {
            model.status = "Set a host address first"
            model.showSettings = true
            return
        }
        model.status = "Opening Moonlight…"
        UIApplication.shared.open(url) { ok in
            Task { @MainActor in
                if ok {
                    model.status = "Handed off to Moonlight — stream Desktop, Cursor is focused on the PC"
                } else if let sunshine = model.sunshineURL() {
                    model.status = "Moonlight not installed — opening Sunshine web"
                    webURL = sunshine
                    showWeb = true
                } else {
                    model.status = "Could not open connection"
                }
            }
        }
    }

    private func openSunshineWeb() {
        guard let url = model.sunshineURL() else {
            model.status = "Set a host address first"
            model.showSettings = true
            return
        }
        webURL = url
        showWeb = true
    }
}

#Preview {
    ContentView()
        .environmentObject(DeskModel())
}
