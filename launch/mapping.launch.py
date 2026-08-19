#!/usr/bin/env python3
"""First manual run: sensors + SLAM Toolbox + route recording services."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import EnvironmentVariable, LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    robot_share = FindPackageShare('robotlidar')
    sensors_launch = PathJoinSubstitution([robot_share, 'launch', 'tractor_sensors.launch.py'])
    slam_launch = PathJoinSubstitution([FindPackageShare('slam_toolbox'), 'launch', 'online_async_launch.py'])
    default_config = PathJoinSubstitution([robot_share, 'config', 'tractor.yaml'])
    default_ekf = PathJoinSubstitution([robot_share, 'config', 'ekf.yaml'])
    default_slam = PathJoinSubstitution([robot_share, 'config', 'slam.yaml'])

    config = LaunchConfiguration('config')
    ekf_config = LaunchConfiguration('ekf_config')
    slam_config = LaunchConfiguration('slam_config')
    serial_port = LaunchConfiguration('serial_port')
    gps_port = LaunchConfiguration('gps_port')
    start_gps = LaunchConfiguration('start_gps')
    use_esp32_drive = LaunchConfiguration('use_esp32_drive')
    external_esp32_drive = LaunchConfiguration('external_esp32_drive')

    return LaunchDescription([
        DeclareLaunchArgument('config', default_value=default_config),
        DeclareLaunchArgument('ekf_config', default_value=default_ekf),
        DeclareLaunchArgument('slam_config', default_value=default_slam),
        DeclareLaunchArgument('serial_port', default_value='/dev/ldlidar'),
        DeclareLaunchArgument('gps_port', default_value=EnvironmentVariable('ROBOTLIDAR_GPS_PORT', default_value='/dev/ttyS0')),
        DeclareLaunchArgument('start_gps', default_value='true'),
        DeclareLaunchArgument('use_esp32_drive', default_value='false'),
        DeclareLaunchArgument('external_esp32_drive', default_value='false'),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(sensors_launch),
            launch_arguments={
                'config': config,
                'ekf_config': ekf_config,
                'serial_port': serial_port,
                'gps_port': gps_port,
                'start_lidar': 'true',
                'start_imu': 'true',
                'start_gps': start_gps,
                'use_esp32_drive': use_esp32_drive,
                'external_esp32_drive': external_esp32_drive,
            }.items(),
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(slam_launch),
            launch_arguments={
                'slam_params_file': slam_config,
                'use_sim_time': 'false',
                'autostart': 'true',
                'use_lifecycle_manager': 'false',
            }.items(),
        ),
        Node(package='robotlidar', executable='route_recorder_node', name='route_recorder_node', output='screen', parameters=[config], emulate_tty=True),
    ])
