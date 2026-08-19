#!/usr/bin/env python3
"""Run RobotLidar web UI together with the persistent ESP32 drive bridge."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    package_share = FindPackageShare('robotlidar')
    esp32_config = PathJoinSubstitution([package_share, 'config', 'esp32_drive.yaml'])
    esp32_port = LaunchConfiguration('esp32_port')

    return LaunchDescription([
        DeclareLaunchArgument(
            'esp32_port',
            default_value=EnvironmentVariable(
                'ROBOTLIDAR_ESP32_PORT', default_value='/dev/esp32drive'
            ),
        ),
        Node(
            package='robotlidar',
            executable='esp32_track_bridge_node',
            name='esp32_track_bridge_node',
            output='screen',
            parameters=[esp32_config, {'serial_port': esp32_port}],
            emulate_tty=True,
            respawn=True,
            respawn_delay=2.0,
        ),
        Node(
            package='robotlidar',
            executable='robotlidar_web',
            name='robotlidar_web',
            output='screen',
            emulate_tty=True,
            respawn=True,
            respawn_delay=2.0,
        ),
    ])
