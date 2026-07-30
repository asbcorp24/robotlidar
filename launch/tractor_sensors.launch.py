#!/usr/bin/env python3
"""Launch drive GPIO, Hall odometry, MPU6050, EKF and LDROBOT STL-19P."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    package_share = FindPackageShare('robotlidar')
    default_config = PathJoinSubstitution(
        [package_share, 'config', 'tractor.yaml']
    )
    default_ekf = PathJoinSubstitution(
        [package_share, 'config', 'ekf.yaml']
    )
    lidar_launch = PathJoinSubstitution(
        [package_share, 'launch', 'ldrobot_stl19p.launch.py']
    )

    config = LaunchConfiguration('config')
    ekf_config = LaunchConfiguration('ekf_config')
    serial_port = LaunchConfiguration('serial_port')
    start_lidar = LaunchConfiguration('start_lidar')
    start_imu = LaunchConfiguration('start_imu')
    laser_scan_dir = LaunchConfiguration('laser_scan_dir')

    actions = [
        DeclareLaunchArgument('config', default_value=default_config),
        DeclareLaunchArgument('ekf_config', default_value=default_ekf),
        DeclareLaunchArgument('serial_port', default_value='/dev/ldlidar'),
        DeclareLaunchArgument('start_lidar', default_value='true'),
        DeclareLaunchArgument('start_imu', default_value='true'),
        DeclareLaunchArgument(
            'laser_scan_dir',
            default_value='true',
            description='STL-19P scan direction: true counterclockwise',
        ),

        Node(
            package='robotlidar',
            executable='motor_gpio_node',
            name='motor_gpio_node',
            output='screen',
            parameters=[config],
            emulate_tty=True,
        ),
        Node(
            package='robotlidar',
            executable='hall_odometry_node',
            name='hall_odometry_node',
            output='screen',
            parameters=[config],
            emulate_tty=True,
        ),
        Node(
            package='robotlidar',
            executable='mpu6050_node',
            name='mpu6050_node',
            output='screen',
            parameters=[config],
            condition=IfCondition(start_imu),
            emulate_tty=True,
        ),
        Node(
            package='robot_localization',
            executable='ekf_node',
            name='ekf_filter_node',
            output='screen',
            parameters=[ekf_config],
            remappings=[('odometry/filtered', '/odometry/filtered')],
            emulate_tty=True,
        ),

        # ВРЕМЕННЫЕ координаты: заменить после измерения места установки.
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='base_to_imu_tf',
            arguments=[
                '--x', '0.0', '--y', '0.0', '--z', '0.25',
                '--roll', '0.0', '--pitch', '0.0', '--yaw', '0.0',
                '--frame-id', 'base_link',
                '--child-frame-id', 'imu_link',
            ],
            condition=IfCondition(start_imu),
        ),
        Node(
            package='tf2_ros',
            executable='static_transform_publisher',
            name='base_to_laser_tf',
            arguments=[
                '--x', '0.35', '--y', '0.0', '--z', '0.55',
                '--roll', '0.0', '--pitch', '0.0', '--yaw', '0.0',
                '--frame-id', 'base_link',
                '--child-frame-id', 'laser',
            ],
            condition=IfCondition(start_lidar),
        ),
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(lidar_launch),
            launch_arguments={
                'serial_port': serial_port,
                'frame_id': 'laser',
                'laser_scan_dir': laser_scan_dir,
            }.items(),
            condition=IfCondition(start_lidar),
        ),
    ]

    return LaunchDescription(actions)
