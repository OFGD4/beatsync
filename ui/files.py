"""Saving finished files and opening folders."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import tools


def save_folder():
    """Where the Save-as dialog opens: the last folder used, else Downloads, else home."""
    last = tools._load_state().get('last_save_dir')
    for d in (last, Path.home() / 'Downloads', Path.home()):
        if d and Path(d).is_dir():
            return Path(d)
    return Path.home()


def remember_save_folder(folder):
    if folder and os.path.isdir(folder):
        tools._save_state(last_save_dir=str(folder))


def free_name(folder, name):
    """name, or 'stem (1).ext', 'stem (2).ext' … — the first one not already in `folder`
    (so saving never asks to replace an earlier export)."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 1
    while (Path(folder) / candidate).exists():
        candidate = f'{stem} ({n}){ext}'
        n += 1
    return candidate


def place_file(src, dest):
    """Put a finished file at `dest`. On the same drive this is a hard link: instant and no extra
    disk space, even for a multi-GB export (the temp name is simply removed later). Otherwise —
    another drive, FAT/exFAT, a synced folder that refuses links — a normal copy."""
    part = dest + '.beatsync-part'
    try:
        if os.path.exists(part):
            os.remove(part)
        os.link(src, part)
        os.replace(part, dest)
        if os.path.exists(part):                 # src and dest were already the same file
            os.remove(part)
        return
    except OSError:
        try:
            os.remove(part)
        except OSError:
            pass
    shutil.copyfile(src, dest)


def reveal(path):
    """Show a file in Explorer / Finder."""
    if not path or not os.path.exists(path):
        return False
    if os.name == 'nt':
        subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', path])
    else:
        subprocess.Popen(['xdg-open', os.path.dirname(path)])
    return True


def open_folder(target):
    if not target:
        return False
    Path(target).mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        os.startfile(str(target))                    # noqa: S606 (local folder)
    else:
        subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(target)])
    return True
