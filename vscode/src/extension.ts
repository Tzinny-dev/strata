import * as vscode from 'vscode';
import * as path from 'path';
import { LanguageClient, LanguageClientOptions, ServerOptions } from 'vscode-languageclient/node';

let client: LanguageClient | undefined;

export function activate(context: vscode.ExtensionContext) {
  const config = vscode.workspace.getConfiguration('strata');
  const strataPath: string = config.get('binaryPath', 'strata');
  // Allow absolute path or binary on PATH; strata lsp is the stdio LSP entry (strata/lsp.py:243)
  const command = strataPath;
  const args = ['lsp'];

  // If user has a venv, prefer that binary — discover via workspace
  // Fallback: try `python -m strata lsp` if `strata` not found — handled by shell error message
  const serverOptions: ServerOptions = {
    command,
    args,
    options: { cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath }
  };

  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: 'file', language: 'strata' }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher('**/*.strata')
    },
    outputChannelName: 'Strata LSP',
    revealOutputChannelOn: 4 // Never
  };

  client = new LanguageClient('strata', 'Strata LSP', serverOptions, clientOptions);
  client.start();
  context.subscriptions.push({ dispose: () => client?.stop() });

  // Helper: show diagnostics source on demand
  vscode.commands.registerCommand('strata.showVersion', async () => {
    const { exec } = await import('child_process');
    exec(`${command} --help`, (err, stdout) => {
      if (err) {
        vscode.window.showErrorMessage(`Strata not found at "${command}": ${err.message}. Set strata.binaryPath in settings.`);
      } else {
        vscode.window.showInformationMessage(stdout.split('\n')[0] || 'Strata ready');
      }
    });
  });
}

export function deactivate(): Thenable<void> | undefined {
  if (!client) return undefined;
  return client.stop();
}
