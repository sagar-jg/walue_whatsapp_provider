from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

setup(
    name="walue_whatsapp_provider",
    version="0.0.1",
    description="WhatsApp Calling & Messaging Provider Platform for Frappe/ERPNext",
    author="Walue Biz",
    author_email="support@walue.biz",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
)
