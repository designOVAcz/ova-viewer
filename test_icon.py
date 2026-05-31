#!/usr/bin/env python3
"""
Test script to verify icon detection logic for PyInstaller
"""
import os
import sys

print("=== Icon Detection Test ===")
print(f"sys.frozen: {getattr(sys, 'frozen', False)}")

if getattr(sys, 'frozen', False):
    # Running as PyInstaller executable
    base_path = sys._MEIPASS
    print(f"Running as executable, base path: {base_path}")
else:
    # Running as Python script
    base_path = os.path.dirname(__file__)
    print(f"Running as script, base path: {base_path}")

# Check for icon files
icon_files = ["icon.svg", "icon.png", "icon.ico"]
for filename in icon_files:
    icon_path = os.path.join(base_path, filename)
    exists = os.path.exists(icon_path)
    print(f"  {filename}: {'✅ Found' if exists else '❌ Missing'} - {icon_path}")

print("\nDirectory contents:")
try:
    files = os.listdir(base_path)
    for f in sorted(files):
        if f.startswith('icon'):
            print(f"  {f}")
except Exception as e:
    print(f"Error listing directory: {e}")
