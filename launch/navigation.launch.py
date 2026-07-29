#!/usr/bin/env python3
"""Autonomous run: sensors + saved map localization + Nav2 + route player."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    robot_share = FindPackageShare('robotlidar')
    sensors_launch = PathJoinSubstitution(
        [robot_share, 'launch', 'tractor_sensors.launch.py']
    )
    nav2_launch = PathJoinSubstitution(
        [FindPackageShare('nav2_bringup'), 'launch', 'bringup_launch.py']
    )

    default_config = PathJoinSubstitution(
        [robot_share, 'config', 'tractor.yaml']
    )
    default_ekf = PathJoinSubstitution(
        [robot_share, 'config', 'ekf.yaml']
    )
    default_nav2 = PathJoinSubstitution(
        [robot_share, 'config', 'nav2.yaml']
    )

    config = LaunchConfiguration('config')
    ekf_config = LaunchConfiguration('ekf_config')
    nav2_config = LaunchConfiguration('nav2_config')
    map_file = LaunchConfiguration('map')
    serial_port = LaunchConfiguration('serial_port')

    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=default_config),
        DeclareLaunchArgument('ekf_config', default_value=default_ekf),
        DeclareLaunchArgument('nav2_config', default_value=default_nav2),
        DeclareLaunchArgument(
            'map',
            default_value='',
            description='Absolute path to the saved map YAML',
        ),
        DeclareLaunchArgument('serial_port', default_value='/dev/ttyUSB0'),

        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(sensors_launch),
            launch_arguments={
                'config': config,
                'ekf_config': ekf_config,
                'serial_port': serial_port,
                'start_lidar': 'true',
                'start_imu': 'true',
            }.items(),
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(nav2_launch),
            launch_arguments={
                'slam': 'false',
                'map': map_file,
                'use_sim_time': 'false',
                'params_file': nav2_config,
                'autostart': 'true',
                'use_composition': 'false',
                'use_respawn': 'true',
                'use_localization': 'true',
            }.items(),
        ),
        Node(
            package='robotlidar',
            executable='route_player_node',
            name='route_player_node',
            output='screen',
            parameters=[config],
            emulate_tty=True,
        ),
    ])
