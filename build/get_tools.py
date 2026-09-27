"""
Download tools into build/bin so they ship inside the installer.

    python build/get_tools.py rubberband          (default — small, never needs updating)
    python build/get_tools.py all                 (offline installer: + ffmpeg, yt-dlp, deno)

Tools shipped this way are found as "built-in". yt-dlp still prefers the
self-updating copy the app downloads into the user folder.
"""
import os
import sys
import time
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
import tools  # noqa: E402

OUT = os.path.join(ROOT, 'build', 'bin')


def main(argv):
    names = argv or ['rubberband']
    if names == ['all']:
        names = ['rubberband', 'ffmpeg', 'yt-dlp', 'deno']
    os.makedirs(OUT, exist_ok=True)
    ok = True
    for name in names:
        if name not in tools.TOOLS:
            print(f'unknown tool: {name}')
            ok = False
            continue
        print(f'-> {name}')
        done = threading.Event()

        def show():
            last = ''
            while not done.is_set():
                msg = tools._jobs.get(name, {}).get('msg', '')
                if msg and msg != last:
                    print('   ' + msg)
                    last = msg
                time.sleep(1)
        threading.Thread(target=show, daemon=True).start()
        try:
            print('   ' + tools.install(name, target_dir=OUT))
        except Exception as e:
            print(f'   FAILED: {e}')
            ok = False
        finally:
            done.set()
    print('bundled tools in', OUT, ':', ', '.join(sorted(os.listdir(OUT))) or '(none)')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
