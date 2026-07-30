from glob import glob
from setuptools import find_packages, setup

package_name = 'robotlidar'

setup(
    name=package_name,
    version='0.3.1',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        (
            'share/' + package_name,
            ['package.xml', 'WEB.md', 'OFFLINE.md', 'STL19P_USB_BOARD.md'],
        ),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/web/static', glob('web/static/*')),
        ('share/' + package_name + '/systemd', glob('systemd/*')),
        ('share/' + package_name + '/scripts', glob('scripts/*')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='asbcorp24',
    maintainer_email='asbcorp24@users.noreply.github.com',
    description=(
        'Offline tracked tractor control, Hall/MPU6050 odometry, '
        'LDROBOT STL-19P, SLAM, Nav2 and local web control panel.'
    ),
    license='MIT',
    entry_points={
        'console_scripts': [
            'motor_gpio_node = robotlidar.motor_gpio_node:main',
            'hall_odometry_node = robotlidar.hall_odometry_node:main',
            'mpu6050_node = robotlidar.mpu6050_node:main',
            'route_recorder_node = robotlidar.route_recorder_node:main',
            'route_player_node = robotlidar.route_player_node:main',
            'robotlidar_web = robotlidar.web_app:main',
        ],
    },
)
