from setuptools import setup

with open("requirements.txt", "r") as f:
    # using the same libraries in requirements.txt because after many articles I didn't understand any good reason why I shouldn't
    requirements = f.read()

setup(
    name='convey',
    version='0.8.0.dev1',
    packages=['convey'],
    author='Edvard Rejthar',
    author_email='edvard.rejthar@nic.cz',
    # summary = 'Just another Python package for the cheese shop',
    url='https://github.com/CZ-NIC/convey',
    license='GNU GPLv3',
    description='CSV swiss knife brought by CSIRT.cz. Convenable way to process large files that might freeze your spreadsheet processor.',
    install_requires=[requirements.split("\n")],
    entry_points={
        'console_scripts': [
            'convey = convey.__main__:main',
        ],
    },
    package_data={'convey': ['config.ini.default']},
    include_package_data=True,
)
