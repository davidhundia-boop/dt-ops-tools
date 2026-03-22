"""
Build a standalone .exe for Campaign Optimization Tool.
Run: pip install pyinstaller && python build_exe.py
Output: dist/Campaign Optimization Tool.exe (double-click to run)
"""

import subprocess
import sys
import os

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller", "-q"])
    subprocess.check_call([
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "Campaign Optimization Tool",
        "app.py",
    ])
    print("Done. Run: dist\\Campaign Optimization Tool.exe")

if __name__ == "__main__":
    main()
