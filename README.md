# OpenCodeSublime

Use [OpenCode](https://opencode.ai/) inside Sublime Text, with your code on the left and an agent panel on the right.

Write messages, attach project files, follow the agent's progress, and continue previous conversations without leaving the editor.

## Features

- Chat and message input in the same side panel.
- Agent selection, including Plan and Build when available.
- Model selection from your configured OpenCode providers.
- Light and Dark themes.
- Project file suggestions using `@`.
- Collapsible thinking output when provided by the model.
- Tool activity, elapsed time, retries, errors, and permission requests.
- Message timestamps.
- Persistent project sessions and unsent drafts.
- Session selection and renaming.
- Copy buttons for responses and fenced code blocks.
- Recorded file changes with read-only diff views.
- Automatic startup of the local OpenCode server when needed.

## Requirements

- **Sublime Text 4, build 4213 or newer**, with its Python 3.14 plugin runtime.
- **OpenCode CLI**, installed separately.
- A model provider configured in OpenCode.

This release was developed and tested on **Windows** and includes Windows key bindings. Other platforms have not been verified.

You do not need to install Python separately for this plugin.

## 1. Set up OpenCode

Follow the [OpenCode installation guide](https://opencode.ai/docs/).

Verify that OpenCode is available from your terminal:

```sh
opencode --version
```

Launch OpenCode and configure a provider if you have not already done so:

```sh
opencode
```

Inside OpenCode, use `/connect` and follow the instructions for your provider.

The plugin uses your existing OpenCode configuration. Provider login and API key setup happen in OpenCode, rather than in the Sublime panel.

## 2. Install from GitHub

### Option A: Download the ZIP

1. Open this repository on GitHub.
2. Select **Code → Download ZIP**.
3. Extract the ZIP.
4. In Sublime Text, select **Preferences → Browse Packages…**.
5. Copy the extracted repository folder into the `Packages` directory and rename it to **OpenCodeSublime**.
6. Restart Sublime Text.

The plugin files must be directly inside `Packages/OpenCodeSublime/`. For example, this file must exist:

```text
Packages/OpenCodeSublime/opencode_sublime.py
```

Keep the included `.python-version` file as well.

Avoid nesting the plugin inside another folder such as `OpenCodeSublime/OpenCodeSublime/`. The package folder must be named **OpenCodeSublime**, because the plugin uses that name to locate its color schemes.

### Option B: Clone with Git

In Sublime Text, select **Preferences → Browse Packages…**, then open a terminal in that directory.

Replace `YOUR_USERNAME` and `YOUR_REPOSITORY` with this repository's GitHub owner and name:

```sh
git clone https://github.com/YOUR_USERNAME/YOUR_REPOSITORY.git OpenCodeSublime
```

Restart Sublime Text.

## 3. Open the agent

1. Open your project folder using **File → Open Folder…**.
2. Open the Command Palette with **Ctrl+Shift+P**.
3. Run **OpenCode: Agent**.

You can also press **Ctrl+Alt+O** on Windows.

The plugin creates a two-column layout: your editor on the left and the OpenCode panel on the right. Write your message under **Your message** in the panel.

## Sending messages

1. Choose an agent, such as **Plan** or **Build**.
2. Choose a model, or leave the model on **Auto**.
3. Select **Light** or **Dark**.
4. Write your request.
5. Click **Send** or press **Ctrl+Enter**.

Example:

```text
Explain how authentication works in this project.
```

Plain **Enter** inserts a new line, except when the file suggestion menu is open.

Your last agent, model selection, and color theme are remembered.

## Adding files with @

Type `@` after a space or at the beginning of your message, then start typing a project file name.

Choose a suggestion with **Enter**, **Tab**, or a mouse click. The selected file will be included as context when you send the message.

Example:

```text
Review @src/main.py and suggest improvements.
```

Files with spaces in their paths are inserted with quotes automatically.

The file list comes from the current project folder. Common generated folders, such as `node_modules`, `.venv`, `dist`, and `build`, are excluded by default.

After adding new files, run **OpenCode: Refresh Project Files** from the Command Palette if they do not appear.

## Following the agent's progress

The panel displays available response text, tool activity, elapsed time, and execution status while OpenCode works.

- Click the thinking toggle to expand or collapse reasoning text returned by the model.
- If OpenCode asks for permission, choose **Allow once** or **Reject**.
- If the agent asks a question, use **Answer** or **Skip**.
- Click **Stop** to request cancellation of the current run.

Thinking output is only available when the model and provider return it.

**Close** closes the panel and restores the previous layout. It does not cancel an agent run on the server. Use **Stop** first if you want to cancel the run.

## Continuing and renaming sessions

The plugin remembers the last selected session and unsent draft for each project folder. Reopening the panel attempts to restore that conversation.

- Click the **Session** title to browse conversations for the current project.
- Click **New** to start a separate conversation.
- Click **Rename** to change the current session's title.

Starting a new conversation does not delete previous sessions.

Conversation history is stored by OpenCode. The plugin stores the project-to-session association and draft in Sublime's user settings. Sessions are associated with project folders, rather than individual source files.

Stop an active run before switching sessions or starting a new one.

## Copying responses and code

- **Copy response** copies the complete response, including its Markdown.
- **Copy code** copies only the contents of that fenced code block, preserving indentation.

## Reviewing file changes

Click **Changes** below the session title to expand the recorded changes for that session.

Each entry shows the file name, change type, and added/deleted line counts.

- **Diff** opens the recorded text diff in a read-only tab in the left editor group.
- **Open** opens the current project file.
- **Refresh** reloads the changes list.

If the changes panel is open, it refreshes automatically when the agent finishes.

This list contains changes reported by OpenCode for the session. It is not a complete list of all uncommitted changes in your project. Some files may have no text diff available.

## Server connection

The plugin connects to:

```text
http://127.0.0.1:4096
```

It reuses a healthy OpenCode server at that address. If none is available, it attempts to start one automatically.

To start the server yourself, open a terminal in your project directory and run:

```sh
opencode serve --hostname 127.0.0.1 --port 4096
```

If your server uses authentication, Sublime must inherit the matching `OPENCODE_SERVER_PASSWORD` environment variable and, if customized, `OPENCODE_SERVER_USERNAME`.

## Settings

To customize the plugin:

1. Select **Preferences → Browse Packages…**.
2. Open the `User` folder.
3. Create or edit `OpenCodeSublime.sublime-settings`.

For example:

```json
{
    "color_mode": "dark",
    "opencode_executable": "C:/Users/YOUR_USERNAME/scoop/shims/opencode.exe"
}
```

Replace the example path with the actual path to your OpenCode executable. Leave `opencode_executable` as `null`, or omit it, to use automatic detection.

Edit the settings in the **User** folder so your preferences remain separate from the installed plugin files. If the file already contains settings, merge your changes into the existing JSON object.

## Updating

For a ZIP installation, download the latest source and replace the files inside `Packages/OpenCodeSublime/`.

For a Git installation, open a terminal in that folder and run:

```sh
git pull
```

Restart Sublime Text after updating.

## Troubleshooting

| Problem | What to check |
| --- | --- |
| The OpenCode commands do not appear | Confirm the package folder name and file placement, keep `.python-version`, and check your Sublime build. Restart Sublime. |
| OpenCode executable not found | Install OpenCode, restart Sublime after changing PATH, or set `opencode_executable` to its full path. |
| The panel cannot connect | Confirm that port 4096 is available, or start the server manually with the command above. |
| No usable models appear | Configure a provider in OpenCode, then close and reopen the panel. |
| File suggestions are missing | Open a project folder and run **OpenCode: Refresh Project Files**. Check whether the file is in an excluded folder. |
| No changes are listed | OpenCode may not have recorded file changes for this session. |
| A deleted file cannot be opened | Use **Diff** to inspect its recorded changes. |

If automatic server startup fails, check `OpenCodeSublime/server.log` inside Sublime's cache directory. Plugin errors can also be inspected through **View → Show Console**.

## Useful links

- [OpenCode documentation](https://opencode.ai/docs/)
- [OpenCode server documentation](https://opencode.ai/docs/server/)
- [Sublime Text downloads](https://www.sublimetext.com/download)

