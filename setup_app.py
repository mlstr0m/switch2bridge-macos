"""
py2app setup for Switch2 Bridge

Usage:
    python setup_app.py py2app
"""

import re

from setuptools import setup

APP = ['Switch2Bridge.py']
ICON = 'AppIcon.icns'

# Single source of truth for the version (avoids importing the app module,
# which has import-time side effects)
with open('Switch2Bridge.py') as f:
    VERSION = re.search(r'^APP_VERSION = "(.+)"', f.read(), re.M).group(1)

OPTIONS = {
    'argv_emulation': False,
    'iconfile': ICON,
    'plist': {
        'CFBundleName': 'Switch2 Bridge',
        'CFBundleDisplayName': 'Switch2 Bridge',
        'CFBundleIdentifier': 'com.aureliendesert.switch2bridge',
        'CFBundleVersion': VERSION,
        'CFBundleShortVersionString': VERSION,
        'CFBundleIconFile': 'AppIcon',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,  # Menubar only, no dock icon
        'NSBluetoothAlwaysUsageDescription': 
            'Switch2 Bridge needs Bluetooth to connect to your controller.',
        'NSBluetoothPeripheralUsageDescription': 
            'Switch2 Bridge needs Bluetooth to connect to your controller.',
        'NSAccessibilityUsageDescription': 
            'Switch2 Bridge needs accessibility access to simulate keyboard input for games.',
    },
    'packages': ['bleak', 'pynput', 'rumps', 'objc'],
    'includes': ['Foundation', 'AppKit', 'CoreBluetooth', 'ApplicationServices',
                 'ServiceManagement', 'dsu_server'],
}

setup(
    app=APP,
    name='Switch2 Bridge',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
