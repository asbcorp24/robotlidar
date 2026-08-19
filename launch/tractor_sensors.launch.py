#!/usr/bin/env python3
"""Launch drive backend, odometry, MPU6500, optional GPS, EKF and STL-19P."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition, UnlessCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import (
    EnvironmentVariable,
    LaunchConfiguration,
    PathJoinSubstitution,
    PythonExpression,
)
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    package_share = FindPackageShare('robotlidar')
    default_config = PathJoinSubstitution([package_share, 'config', 'tractor.yaml'])
    default_ekf = PathJoinSubstitution([package_share, 'config', 'ekf.yaml'])
    default_esp32_config = PathJoinSubstitution([package_share, 'config', 'esp32_drive.yaml'])
    lidar_launch = PathJoinSubstitution([package_share, 'launch', 'ldrobot_stl19p.launch.py'])

    config = LaunchConfiguration('config')
    ekf_config = LaunchConfiguration('ekf_config')
    esp32_config = LaunchConfiguration('esp32_config')
    serial_port = LaunchConfiguration('serial_port')
    gps_port = LaunchConfiguration('gps_port')
    start_lidar = LaunchConfiguration('start_lidar')
    start_imu = LaunchConfiguration('start_imu')
    start_gps = LaunchConfiguration('start_gps')
    use_esp32_drive = LaunchConfiguration('use_esp32_drive')
    external_esp32_drive = LaunchConfiguration('external_esp32_drive')
    laser_scan_dir = LaunchConfiguration('laser_scan_dir')

    start_internal_esp32 = IfCondition(
        PythonExpression([
            "'", use_esp32_drive, "' == 'true' and '",
            external_esp32_drive, "' != 'true'",
        ])
    )

    actions = [
        DeclareLaunchArgument('config', default_value=default_config),
        DeclareLaunchArgument('ekf_config', default_value=default_ekf),
        DeclareLaunchArgument('esp32_config', default_value=default_esp32_config),
        DeclareLaunchArgument('serial_port', default_value='/dev/ldlidar'),
        DeclareLaunchArgument(
            'gps_port',
            default_value=EnvironmentVariable('ROBOTLIDAR_GPS_PORT', default_value='/dev/ttyS0'),
        ),
        DeclareLaunchArgument('start_lidar', default_value='true'),
        DeclareLaunchArgument('start_imu', default_value='true'),
        DeclareLaunchArgument('start_gps', default_value='true'),
        DeclareLaunchArgument('use_esp32_drive', default_value='false'),
        DeclareLaunchArgument(
            'external_esp32_drive',
            default_value='false',
            description='true: ESP32 bridge and ESP32 odometry are already running in web_stack',
        ),
        DeclareLaunchArgument('laser_scan_dir', default_value='true'),

        Node(package='robotlidar', executable='motor_gpio_node', name='motor_gpio_node', output='screen', parameters=[config], condition=UnlessCondition(use_esp32_drive), emulate_tty=True),
        Node(package='robotlidar', executable='hall_odometry_node', name='hall_odometry_node', output='screen', parameters=[config], condition=UnlessCondition(use_esp32_drive), emulate_tty=True),
        Node(package='robotlidar', executable='esp32_track_bridge_node', name='esp32_track_bridge_node', output='screen', parameters=[config, esp32_config], condition=start_internal_esp32, emulate_tty=True),
        Node(package='robotlidar', executable='esp32_track_odometry_node', name='esp32_track_odometry_node', output='screen', parameters=[config, esp32_config], condition=start_internal_esp32, emulate_tty=True),
        Node(package='robotlidar', executable='mpu6050_node', name='mpu6050_node', output='screen', parameters=[config], condition=IfCondition(start_imu), emulate_tty=True),
        Node(package='robotlidar', executable='gps_node', name='gps_node', output='screen', parameters=[config, {'port': gps_port}], condition=IfCondition(start_gps), emulate_tty=True),
        Node(package='robot_localization', executable='ekf_node', name='ekf_filter_node', output='screen', parameters=[ekf_config], remappings=[('odometry/filtered', '/odometry/filtered')], emulate_tty=True),
        Node(package='tf2_ros', executable='static_transform_publisher', name='base_to_imu_tf', arguments=['--x','0.0','--y','0.0','--z','0.25','--roll','0.0','--pitch','0.0','--yaw','0.0','--frame-id','base_link','--child-frame-id','imu_link'], condition=IfCondition(start_imu)),
        Node(package='tf2_ros', executable='static_transform_publisher', name='base_to_gps_tf', arguments=['--x','0.0','--y','0.0','--z','0.75','--roll','0.0','--pitch','0.0','--yaw','0.0','--frame-id','base_link','--child-frame-id','gps_link'], condition=IfCondition(start_gps)),
        Node(package='tf2_ros', executable='static_transform_publisher', name='base_to_laser_tf', arguments=['--x','0.35','--y','0.0','--z','0.55','--roll','0.0','--pitch','0.0','--yaw','0.0','--frame-id','base_link','--child-frame-id','laser'], condition=IfCondition(start_lidar)),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(lidar_launch),
            launch_arguments={'serial_port': serial_port, 'frame_id': 'laser', 'laser_scan_dir': laser_scan_dir}.items(),
            condition=IfCondition(start_lidar),
        ),
    ]
    return LaunchDescription(actions)
