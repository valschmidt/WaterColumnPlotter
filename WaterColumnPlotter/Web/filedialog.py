"""
The operating system's own Open dialog, shown on the plotter's computer, for choosing a recording to replay.

A web page can't learn the path of a file picked in the browser's file dialog, and the replay reads the file from the
plotter's disk, so the server opens the dialog itself: on macOS through AppleScript (the Finder's Open panel), on
Windows through PowerShell (the Explorer Open dialog), on Linux through zenity (GNOME) or kdialog (KDE), and
otherwise through Tk. Each runs as a separate process, so the dialog doesn't tie up the server and needs no GUI event
loop in it.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

from WaterColumnPlotter.Web.replay import EXTENSIONS

TITLE = "Choose a recording to replay"


class Unavailable(Exception):
    """No native dialog can be shown on this computer."""


_MACOS = """
on run argv
    activate
    try
        set f to choose file with prompt (item 1 of argv) of type {%s} default location (POSIX file (item 2 of argv))
    on error number -128
        return ""
    end try
    return POSIX path of f
end run
""" % ', '.join(f'"{e[1:]}"' for e in EXTENSIONS)

# The dialog gets a topmost owner window so it opens in front of the browser.
_WINDOWS = """
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object System.Windows.Forms.OpenFileDialog
$d.Title = $env:WCP_DIALOG_TITLE
$d.InitialDirectory = $env:WCP_DIALOG_DIR
$d.Filter = $env:WCP_DIALOG_FILTER
$owner = New-Object System.Windows.Forms.Form -Property @{TopMost = $true}
if ($d.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.FileName) }
"""

_TK = """
import sys, tkinter
from tkinter import filedialog
root = tkinter.Tk()
root.withdraw()
root.attributes('-topmost', True)
path = filedialog.askopenfilename(parent=root, title=sys.argv[1], initialdir=sys.argv[2],
                                  filetypes=[('Recordings', sys.argv[3]), ('All files', '*')])
sys.stdout.write(path or '')
"""


def _command(start_dir):
    """
    (argv, extra environment, exit codes meaning Cancel) for this platform's dialog. It prints the chosen path, or
    nothing if cancelled.
    """
    patterns = ' '.join('*' + e for e in EXTENSIONS)
    if sys.platform == 'darwin':
        return ['osascript', '-e', _MACOS, TITLE, start_dir], {}, (0,)
    if sys.platform == 'win32':
        shell = shutil.which('powershell') or shutil.which('pwsh')
        if shell:
            return ([shell, '-NoProfile', '-NonInteractive', '-STA', '-Command', _WINDOWS],
                    {'WCP_DIALOG_TITLE': TITLE, 'WCP_DIALOG_DIR': start_dir,
                     'WCP_DIALOG_FILTER': f"Recordings ({patterns})|{patterns.replace(' ', ';')}|All files (*.*)|*.*"}, (0,))
    elif os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        if shutil.which('zenity'):
            return ['zenity', '--file-selection', f'--title={TITLE}', f'--filename={start_dir}{os.sep}',
                    f'--file-filter=Recordings | {patterns}', '--file-filter=All files | *'], {}, (0, 1)
        if shutil.which('kdialog'):
            return ['kdialog', '--title', TITLE, '--getopenfilename', start_dir, f'{patterns}|Recordings'], {}, (0, 1)
    else:
        raise Unavailable("there is no display to show a dialog on")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise Unavailable("no file dialog is available") from None
    return [sys.executable, '-c', _TK, TITLE, start_dir, patterns], {}, (0,)


async def choose_recording(start_dir):
    """
    Show the dialog, starting in start_dir (or the home directory if that's gone), and wait for it to close.
    Returns the chosen file's path, or None if the dialog was cancelled.
    :raises Unavailable: if no dialog can be shown.
    """
    start = Path(start_dir) if start_dir and Path(start_dir).is_dir() else Path.home()
    argv, env, cancel_codes = _command(str(start))
    try:
        proc = await asyncio.create_subprocess_exec(*argv, env={**os.environ, **env}, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise Unavailable(f"can't start {argv[0]}: {e}") from None
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:  # the page that asked went away (or the server is stopping): close the dialog
        proc.kill()
        raise
    path = out.decode('utf-8', 'replace').strip()
    if proc.returncode not in cancel_codes:  # zenity and kdialog exit with 1 on Cancel
        raise Unavailable(err.decode('utf-8', 'replace').strip() or f"{argv[0]} exited with {proc.returncode}")
    return path or None
