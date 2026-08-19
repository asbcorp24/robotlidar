#!/usr/bin/env python3
"""Run persistent RobotLidar control, positioning and web services."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    package_share = FindPackageShare('robotlidar')
    esp32_config = PathJoinSubstitution([package_share, 'config', 'esp32_drive.yaml'])
    tractor_config = PathJoinSubstitution([package_share, 'config', 'tractor.yaml'])
    esp32_port = LaunchConfiguration('esp32_port')
    gps_port = LaunchConfiguration('gps_port')

    return LaunchDescription([
        DeclareLaunchArgument(
            'esp32_port',
            default_value=EnvironmentVariable('ROBOTLIDAR_ESP32_PORT', default_value='/dev/esp32drive'),
        ),
        DeclareLaunchArgument(
            'gps_port',
            default_value=EnvironmentVariable('ROBOTLIDAR_GPS_PORT', default_value='/dev/ttyS0'),
        ),
        Node(
            package='robotlidar', executable='esp32_track_bridge_node', name='esp32_track_bridge_node',
            output='screen', parameters=[esp32_config, {'serial_port': esp32_port}], emulate_tty=True,
            respawn=True, respawn_delay=2.0,
        ),
        Node(
            package='robotlidar', executable='esp32_track_odometry_node', name='esp32_track_odometry_node',
            output='screen', parameters=[tractor_config, esp32_config], emulate_tty=True,
            respawn=True, respawn_delay=2.0,
        ),
        Node(
            package='robotlidar', executable='gps_node', name='gps_node', output='screen',
            parameters=[tractor_config, {'port': gps_port}], emulate_tty=True,
            respawn=True, respawn_delay=2.0,
        ),
        Node(
            package='tf2_ros', executable='static_transform_publisher', name='base_to_gps_tf',
            arguments=[
                '--x', '0.0', '--y', '0.0', '--z', '0.75',
                '--roll', '0.0', '--pitch', '0.0', '--yaw', '0.0',
                '--frame-id', 'base_link', '--child-frame-id', 'gps_link',
            ],
        ),
        Node(
            package='robotlidar', executable='position_state_node', name='position_state_node',
            output='screen', emulate_tty=True, respawn=True, respawn_delay=2.0,
        ),
        Node(
            package='robotlidar', executable='robotlidar_web', name='robotlidar_web',
            output='screen', emulate_tty=True, respawn=True, respawn_delay=2.0,
        ),
    ])
