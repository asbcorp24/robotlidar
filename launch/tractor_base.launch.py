#!/usr/bin/env python3

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    default_config = PathJoinSubstitution(
        [FindPackageShare('robotlidar'), 'config', 'tractor.yaml']
    )

    config_argument = DeclareLaunchArgument(
        'config',
        default_value=default_config,
        description='Path to tractor ROS 2 parameter file',
    )

    motor_node = Node(
        package='robotlidar',
        executable='motor_gpio_node',
        name='motor_gpio_node',
        output='screen',
        parameters=[LaunchConfiguration('config')],
        emulate_tty=True,
    )

    hall_node = Node(
        package='robotlidar',
        executable='hall_odometry_node',
        name='hall_odometry_node',
        output='screen',
        parameters=[LaunchConfiguration('config')],
        emulate_tty=True,
    )

    return LaunchDescription([
        config_argument,
        motor_node,
        hall_node,
    ])
