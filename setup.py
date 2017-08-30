from setuptools import setup


setup(
    name='milldeploy',
    version='0.2.0',
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
