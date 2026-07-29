from glob import glob
from setuptools import find_packages, setup

package_name = 'robotlidar'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='asbcorp24',
    maintainer_email='asbcorp24@users.noreply.github.com',
    description='Offline tracked tractor control and Hall sensor odometry.',
    license='MIT',
    entry_points={
        'console_scripts': [
            'motor_gpio_node = robotlidar.motor_gpio_node:main',
            'hall_odometry_node = robotlidar.hall_odometry_node:main',
        ],
    },
)
