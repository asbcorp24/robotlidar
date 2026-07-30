#!/usr/bin/env python3
"""LDROBOT STL-19P / D500 driver using the LD19 ROS 2 profile."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    serial_port = LaunchConfiguration('serial_port')
    frame_id = LaunchConfiguration('frame_id')
    laser_scan_dir = LaunchConfiguration('laser_scan_dir')
    enable_angle_crop = LaunchConfiguration('enable_angle_crop')
    angle_crop_min = LaunchConfiguration('angle_crop_min')
    angle_crop_max = LaunchConfiguration('angle_crop_max')

    return LaunchDescription([
        DeclareLaunchArgument(
            'serial_port',
            default_value='/dev/ttyUSB0',
            description='Serial port of LDROBOT STL-19P',
        ),
        DeclareLaunchArgument(
            'frame_id',
            default_value='laser',
            description='LaserScan frame',
        ),
        DeclareLaunchArgument(
            'laser_scan_dir',
            default_value='true',
            description='true: counterclockwise, false: clockwise',
        ),
        DeclareLaunchArgument(
            'enable_angle_crop',
            default_value='false',
            description='Mask measurements inside the configured angle interval',
        ),
        DeclareLaunchArgument('angle_crop_min', default_value='135.0'),
        DeclareLaunchArgument('angle_crop_max', default_value='225.0'),
        Node(
            package='ldlidar_stl_ros2',
            executable='ldlidar_stl_ros2_node',
            name='ldrobot_stl19p',
            output='screen',
            parameters=[{
                # STL-19P/D500 uses the LD19 data protocol.
                'product_name': 'LDLiDAR_LD19',
                'topic_name': 'scan',
                'frame_id': frame_id,
                'port_name': serial_port,
                'port_baudrate': 230400,
                'laser_scan_dir': laser_scan_dir,
                'enable_angle_crop_func': enable_angle_crop,
                'angle_crop_min': angle_crop_min,
                'angle_crop_max': angle_crop_max,
            }],
            emulate_tty=True,
        ),
    ])
