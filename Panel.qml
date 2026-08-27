import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "io.github.jakewaynemurray.swing-music"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  property bool configured: false
  property bool checking: true
  property bool busy: false
  property bool playBusy: false
  property bool playerActive: false
  property bool playerPaused: false
  property string playerTitle: ""
  property string playerArtwork: ""
  property string playerTrackHash: ""
  property bool playerFavorite: false
  property bool favoriteBusy: false
  property real playerVolume: 100
  property int playerIndex: 0
  property int playerCount: 0
  property string serverUrl: ""
  property string savedUsername: ""
  property string errorMessage: ""
  property var results: []
  property int selectedIndex: 0
  property string selectedCategory: "all"
  readonly property var searchCategories: [
    { key: "all", label: "All" },
    { key: "track", label: "Songs" },
    { key: "album", label: "Albums" },
    { key: "artist", label: "Artists" },
    { key: "playlist", label: "Playlists" },
    { key: "favorite", label: "♥" }
  ]
  readonly property string helperPath: Qt.resolvedUrl("swing_client.py").toString().replace("file://", "")
  readonly property var barIdentity: hostWidget || root

  function open() {
    root.controller.show()
    if (configured) {
      if (results.length === 0 && searchField.text.trim().length < 2) Qt.callLater(function() { root.runBrowse() })
      Qt.callLater(function() { searchField.forceActiveFocus() })
    }
  }

  function close() {
    root.controller.hide()
  }

  function switchPanel(direction) {
    if (bar && typeof bar.switchPanelFrom === "function")
      return bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function checkStatus() {
    checking = true
    statusProc.running = true
  }

  function refreshPlayerStatus() {
    if (!playerStatusProc.running) playerStatusProc.running = true
  }

  function controlPlayer(action) {
    if (controlProc.running) return
    controlProc.command = [root.helperPath, "control", action]
    controlProc.running = true
  }

  function setPlayerVolume(value) {
    playerVolume = Math.max(0, Math.min(100, Number(value)))
    controlProc.command = [root.helperPath, "control", "volume", String(Math.round(playerVolume))]
    controlProc.running = true
  }

  function togglePlayerFavorite() {
    if (favoriteBusy || playerTrackHash === "") return
    favoriteBusy = true
    favoriteProc.command = [root.helperPath, "favorite", playerTrackHash, playerFavorite ? "remove" : "add"]
    favoriteProc.running = true
  }

  function checkPlayerFavorite(trackHash) {
    if (trackHash === "" || favoriteStatusProc.running) return
    favoriteStatusProc.command = [root.helperPath, "favorite-status", trackHash]
    favoriteStatusProc.running = true
  }

  function parseOutput(text, fallback) {
    try { return JSON.parse(text.trim()) }
    catch (e) { return { ok: false, error: fallback } }
  }

  function applyStatus(data) {
    checking = false
    configured = data.configured === true
    serverUrl = String(data.url || "")
    savedUsername = String(data.username || "")
    if (!configured) Qt.callLater(function() { urlField.forceActiveFocus() })
  }

  function submitSetup() {
    errorMessage = ""
    if (urlField.text.trim() === "" || usernameField.text.trim() === "" || passwordField.text === "") {
      errorMessage = "URL, username, and password are required."
      return
    }
    busy = true
    configureProc.secret = passwordField.text
    configureProc.command = [root.helperPath, "configure"]
    configureProc.running = true
  }

  function runSearch() {
    var query = searchField.text.trim()
    if (!configured) {
      results = []
      busy = false
      return
    }
    if (query.length < 2) {
      runBrowse()
      return
    }
    if (selectedCategory === "favorite") {
      runBrowse()
      return
    }
    busy = true
    errorMessage = ""
    searchProc.command = [root.helperPath, "search", query, root.selectedCategory]
    searchProc.running = true
  }

  function runBrowse() {
    if (!configured || busy || (selectedCategory !== "favorite" && searchField.text.trim().length >= 2)) return
    busy = true
    errorMessage = ""
    browseProc.command = [root.helperPath, "browse", root.selectedCategory]
    browseProc.running = true
  }

  function chooseCategory(category) {
    if (selectedCategory === category) return
    selectedCategory = category
    selectedIndex = 0
    results = []
    if (category === "favorite") {
      searchDebounce.stop()
      searchField.text = ""
      runBrowse()
    } else if (searchField.text.trim().length >= 2) searchDebounce.restart()
    else runBrowse()
  }

  function selectBy(delta) {
    if (results.length === 0) return
    selectedIndex = Math.max(0, Math.min(results.length - 1, selectedIndex + delta))
    resultList.positionViewAtIndex(selectedIndex, ListView.Contain)
  }

  function playResult(index) {
    if (index < 0 || index >= results.length) return
    if (playBusy) return
    playBusy = true
    errorMessage = ""
    playProc.stdinEnabled = true
    playProc.payload = JSON.stringify(results[index])
    playProc.command = [root.helperPath, "play"]
    playProc.running = true
  }

  function openResultPage(index) {
    if (index < 0 || index >= results.length) return
    var url = String(results[index].url || serverUrl)
    openUrl(url)
    close()
  }

  function openUrl(url) {
    if (!url || openProc.running) return
    openProc.command = ["xdg-open", String(url)]
    openProc.running = true
  }

  onOpenedChanged: {
    if (opened) {
      root.refreshPlayerStatus()
      if (configured) Qt.callLater(function() { searchField.forceActiveFocus() })
    }
  }

  Component.onCompleted: {
    checkStatus()
    refreshPlayerStatus()
  }

  Timer {
    id: searchDebounce
    interval: 450
    onTriggered: root.runSearch()
  }

  Timer {
    interval: 800
    repeat: true
    running: root.opened
    triggeredOnStart: true
    onTriggered: root.refreshPlayerStatus()
  }

  Process {
    id: statusProc
    command: [root.helperPath, "status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.applyStatus(root.parseOutput(text, "Could not read Swing Music settings."))
    }
  }

  Process {
    id: configureProc
    property string secret: ""
    stdinEnabled: true
    onStarted: {
      write(JSON.stringify({
        url: urlField.text.trim(),
        username: usernameField.text.trim(),
        password: secret
      }) + "\n")
      secret = ""
      passwordField.text = ""
      stdinEnabled = false
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Setup failed.")
        root.busy = false
        if (!data.ok) {
          root.errorMessage = String(data.error || "Setup failed.")
          return
        }
        root.configured = true
        root.serverUrl = String(data.url || "")
        root.savedUsername = String(data.username || "")
        root.errorMessage = ""
        root.runBrowse()
        Qt.callLater(function() { searchField.forceActiveFocus() })
      }
    }
  }

  Process {
    id: searchProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Search returned invalid data.")
        root.busy = false
        if (!data.ok) {
          root.results = []
          root.errorMessage = String(data.error || "Search failed.")
          return
        }
        root.results = data.results || []
        root.selectedIndex = 0
        root.errorMessage = ""
      }
    }
  }

  Process {
    id: browseProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Could not load your library.")
        root.busy = false
        if (!data.ok) {
          root.results = []
          root.errorMessage = String(data.error || "Could not load your library.")
          return
        }
        root.results = data.results || []
        root.selectedIndex = 0
        root.errorMessage = ""
      }
    }
  }

  Process {
    id: openProc
  }

  Process {
    id: playerStatusProc
    command: [root.helperPath, "player-status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "")
        if (data.ok && data.active) {
          root.playerActive = true
          root.playerPaused = data.paused === true
          root.playerTitle = String(data.title || "")
          root.playerArtwork = String(data.artwork || "")
          var nextTrackHash = String(data.trackhash || "")
          if (nextTrackHash !== root.playerTrackHash) {
            root.playerTrackHash = nextTrackHash
            root.playerFavorite = data.favorite === true
            root.checkPlayerFavorite(nextTrackHash)
          }
          root.playerVolume = Number(data.volume === undefined ? 100 : data.volume)
          root.playerIndex = Number(data.index || 0)
          root.playerCount = Number(data.count || 0)
        } else {
          root.playerActive = false
          root.playerPaused = false
          root.playerTitle = ""
          root.playerArtwork = ""
          root.playerTrackHash = ""
          root.playerFavorite = false
          root.favoriteBusy = false
          root.playerVolume = 100
          root.playerIndex = 0
          root.playerCount = 0
        }
      }
    }
  }

  Process {
    id: controlProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Playback control failed.")
        if (!data.ok) root.errorMessage = String(data.error || "Playback control failed.")
        root.refreshPlayerStatus()
      }
    }
  }

  Process {
    id: favoriteProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Favorite update failed.")
        root.favoriteBusy = false
        if (!data.ok) {
          root.errorMessage = String(data.error || "Favorite update failed.")
          return
        }
        root.playerFavorite = data.favorite === true
        root.errorMessage = ""
        if (root.selectedCategory === "favorite") root.runBrowse()
        root.refreshPlayerStatus()
      }
    }
  }

  Process {
    id: favoriteStatusProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "")
        if (data.ok && data.trackhash === root.playerTrackHash)
          root.playerFavorite = data.favorite === true
      }
    }
  }

  Process {
    id: playProc
    property string payload: ""
    stdinEnabled: true
    onStarted: {
      write(payload + "\n")
      payload = ""
      stdinEnabled = false
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var data = root.parseOutput(text, "Could not start playback.")
        root.playBusy = false
        if (!data.ok) {
          root.errorMessage = String(data.error || "Could not start playback.")
          return
        }
        root.refreshPlayerStatus()
      }
    }
  }

  Process {
    id: stopProc
    command: [root.helperPath, "stop"]
  }

  KeyboardPanel {
    id: popup
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(430))
    contentHeight: popup.fittedContentHeight(content.implicitHeight, Style.space(680))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: urlField.activeFocus || usernameField.activeFocus || passwordField.activeFocus || searchField.activeFocus
      onMoveRequested: function(dx, dy) { if (dy !== 0) root.selectBy(dy) }
      onActivateRequested: root.playResult(root.selectedIndex)
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        Row {
          width: parent.width
          spacing: Style.space(9)

          Text {
            text: "♫"
            color: root.bar ? root.bar.foreground : Color.foreground
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.display
          }

          Column {
            width: parent.width - Style.space(50)
            Text {
              text: "Swing Music"
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.subtitle
              font.bold: true
            }
            Text {
              text: root.configured ? root.savedUsername + " · " + root.serverUrl : "Connect your self-hosted library"
              color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.45)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
              width: parent.width
            }
          }
        }

        Text {
          visible: root.checking
          text: "Checking configuration…"
          color: root.bar ? root.bar.foreground : Color.foreground
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.body
        }

        Column {
          visible: !root.checking && !root.configured
          width: parent.width
          spacing: Style.space(8)

          TextField {
            id: urlField
            width: parent.width
            placeholderText: "Server URL (for example http://localhost:1970)"
            onAccepted: usernameField.forceActiveFocus()
          }
          TextField {
            id: usernameField
            width: parent.width
            placeholderText: "Username"
            onAccepted: passwordField.forceActiveFocus()
          }
          TextField {
            id: passwordField
            width: parent.width
            password: true
            placeholderText: "Password"
            onAccepted: root.submitSetup()
          }
          Button {
            width: parent.width
            text: root.busy ? "Connecting…" : "Connect"
            bordered: true
            focusable: true
            onClicked: if (!root.busy) root.submitSetup()
          }
          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Your password is stored in the desktop keyring, not in Omarchy's configuration."
            color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.55)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        Column {
          visible: !root.checking && root.configured
          width: parent.width
          spacing: Style.space(8)

          TextField {
            id: searchField
            width: parent.width
            placeholderText: "Search songs, albums, artists, playlists…"
            onTextChanged: searchDebounce.restart()
            onAccepted: {
              searchDebounce.stop()
              root.runSearch()
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: root.searchCategories
              delegate: Button {
                required property var modelData
                text: String(modelData.label)
                selected: root.selectedCategory === String(modelData.key)
                bordered: true
                horizontalPadding: Style.space(8)
                verticalPadding: Style.space(4)
                onClicked: root.chooseCategory(String(modelData.key))
              }
            }
          }

          Text {
            visible: root.busy || root.playBusy
            text: root.playBusy ? "Starting playback…" : (searchField.text.trim().length < 2 ? "Loading your library…" : "Searching…")
            color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.35)
            font.family: root.bar ? root.bar.fontFamily : Style.font.family
            font.pixelSize: Style.font.caption
          }

          ListView {
            id: resultList
            width: parent.width
            height: Style.space(360)
            clip: true
            spacing: Style.space(3)
            model: root.results

            delegate: Button {
              required property int index
              required property var modelData
              width: resultList.width
              height: Style.space(50)
              leftAlign: true
              bordered: false
              hasCursor: index === root.selectedIndex
              tooltipText: "Click to play · Right-click to open in Swing"
              onHovered: function(isHovered) { if (isHovered) root.selectedIndex = index }
              onClicked: root.playResult(index)
              onRightClicked: root.openResultPage(index)

              Column {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(10)
                anchors.right: parent.right
                anchors.rightMargin: Style.space(10)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(1)
                Text {
                  width: parent.width
                  text: String(modelData.title || "")
                  color: root.bar ? root.bar.foreground : Color.foreground
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                  elide: Text.ElideRight
                }
                Text {
                  width: parent.width
                  text: String(modelData.typeLabel || "") + (modelData.subtitle ? " · " + modelData.subtitle : "")
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.5)
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }
              }
            }

            Text {
              anchors.centerIn: parent
              visible: !root.busy && root.results.length === 0 && searchField.text.trim().length >= 2 && root.errorMessage === ""
              text: "No matches"
              color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.5)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.body
            }
          }

          Column {
            visible: root.playerActive
            width: parent.width
            spacing: Style.space(8)

            Row {
              width: parent.width
              spacing: Style.space(10)

              Image {
                width: Style.space(72)
                height: Style.space(72)
                source: root.playerArtwork
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                smooth: true
                visible: status === Image.Ready
              }

              Column {
                width: parent.width - Style.space(130)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(3)
                Text {
                  width: parent.width
                  text: root.playerTitle || "Swing Music"
                  color: root.bar ? root.bar.foreground : Color.foreground
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.body
                  font.bold: true
                  elide: Text.ElideRight
                }
                Text {
                  visible: root.playerCount > 0
                  text: "Track " + (root.playerIndex + 1) + " of " + root.playerCount
                  color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.45)
                  font.family: root.bar ? root.bar.fontFamily : Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              Button {
                width: Style.space(32)
                height: Style.space(32)
                text: root.playerFavorite ? "♥" : "♡"
                bordered: true
                enabled: !root.favoriteBusy
                tooltipText: root.playerFavorite ? "Remove from favorites" : "Add to favorites"
                anchors.verticalCenter: parent.verticalCenter
                onClicked: root.togglePlayerFavorite()
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(8)

              Button {
                text: "Previous"
                bordered: true
                width: (parent.width - Style.space(16)) / 3
                onClicked: root.controlPlayer("previous")
              }
              Button {
                text: root.playerPaused ? "Play" : "Pause"
                bordered: true
                width: (parent.width - Style.space(16)) / 3
                onClicked: root.controlPlayer("pause")
              }
              Button {
                text: "Next"
                bordered: true
                width: (parent.width - Style.space(16)) / 3
                onClicked: root.controlPlayer("next")
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(6)
            Button {
              text: "Open Swing"
              bordered: true
              width: (parent.width - Style.space(12)) / 3
              onClicked: {
                root.openUrl(root.serverUrl)
                root.close()
              }
            }
            Button {
              text: "Stop"
              width: (parent.width - Style.space(12)) / 3
              onClicked: {
                stopProc.running = true
                root.playerActive = false
                root.refreshPlayerStatus()
              }
            }
            Button {
              text: "Change connection"
              width: (parent.width - Style.space(12)) / 3
              onClicked: {
                root.configured = false
                urlField.text = root.serverUrl
                usernameField.text = root.savedUsername
                root.results = []
                Qt.callLater(function() { urlField.forceActiveFocus() })
              }
            }
          }

          Row {
            width: parent.width
            spacing: Style.space(8)
            visible: root.playerActive

            Text {
              text: "Volume"
              color: root.bar ? root.bar.foreground : Color.foreground
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              verticalAlignment: Text.AlignVCenter
            }

            PanelSlider {
              id: volumeSlider
              bar: root.bar
              width: parent.width - Style.space(70)
              minimum: 0
              maximum: 100
              step: 1
              integer: true
              value: root.playerVolume
              onMoved: root.playerVolume = liveValue
              onReleased: function(v) { root.setPlayerVolume(v) }
            }

            Text {
              text: Math.round(volumeSlider.dragging ? volumeSlider.liveValue : root.playerVolume) + "%"
              color: Qt.darker(root.bar ? root.bar.foreground : Color.foreground, 1.4)
              font.family: root.bar ? root.bar.fontFamily : Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
              verticalAlignment: Text.AlignVCenter
            }
          }
        }

        Text {
          visible: root.errorMessage !== ""
          width: parent.width
          wrapMode: Text.WordWrap
          text: root.errorMessage
          color: Color.urgent
          font.family: root.bar ? root.bar.fontFamily : Style.font.family
          font.pixelSize: Style.font.caption
        }
      }
    }
  }
}
