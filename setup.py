from setuptools import find_packages, setup

with open("README.md") as readme_file:
    readme = readme_file.read()

with open("requirements.txt") as requirements_file:
    requirements = requirements_file.read().splitlines()

setup(
    author="jackzzs",
    author_email="jackzzs@outlook.com",
    python_requires=">=3.7,<3.11",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
    description="Bot server for iwexchanger_bot.",
    install_requires=requirements,
    long_description=readme,
    long_description_content_type="text/markdown",
    include_package_data=True,
    keywords=["telegram", "bot", "server"],
    name="iwexchanger-bot",
    packages=find_packages(include=["iwexchanger", "iwexchanger.*"]),
    url="https://github.com/jackzzs/iwexchanger-bot",
    version="0.1.0",
    zip_safe=False,
)
