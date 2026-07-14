"""
py2app setup for Switch2 Bridge

Usage:
    python setup_app.py py2app
"""

from setuptools import setup

APP = ['Switch2Bridge.py']
ICON = 'AppIcon.icns'

OPTIONS = {
    'argv_emulation': False,
    'iconfile': ICON,
    'plist': {
        'CFBundleName': 'Switch2 Bridge',
        'CFBundleDisplayName': 'Switch2 Bridge',
        'CFBundleIdentifier': 'com.aureliendesert.switch2bridge',
        'CFBundleVersion': '1.1.0',
        'CFBundleShortVersionString': '1.1.0',
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
    'includes': ['Foundation', 'AppKit', 'CoreBluetooth', 'ApplicationServices'],
}

setup(
    app=APP,
    name='Switch2 Bridge',
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
