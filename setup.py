from setuptools import setup


setup(
    name='milldeploy',
    version='0.5.0-snapshot',
    py_modules=['milldeploy'],
    install_requires=[
        'Click',
        'gitpython',
        'boto3 >= 1.4.6',
    ],
    entry_points='''
        [console_scripts]
        milldeploy=milldeploy:cli
    ''',



)
