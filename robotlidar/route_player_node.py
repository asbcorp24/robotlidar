#!/usr/bin/env python3
"""Play a recorded route using Nav2 NavigateThroughPoses."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import rclpy
import yaml
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateThroughPoses
from rclpy.action import ActionClient
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger


class RoutePlayerNode(Node):
    """Send a recorded cleaning route to Nav2 and expose simple services."""

    def __init__(self) -> None:
        super().__init__('route_player_node')

        self.declare_parameter(
            'route_file', '~/robotlidar_data/routes/cleaning_route.yaml'
        )
        self.declare_parameter('action_name', 'navigate_through_poses')
        self.declare_parameter('server_wait_timeout_sec', 3.0)

        self.route_file = Path(
            str(self.get_parameter('route_file').value)
        ).expanduser()
        action_name = str(self.get_parameter('action_name').value)
        self.server_wait_timeout = float(
            self.get_parameter('server_wait_timeout_sec').value
        )

        self.action_client = ActionClient(
            self, NavigateThroughPoses, action_name
        )
        self.goal_handle = None
        self.state = 'idle'

        self.state_publisher = self.create_publisher(
            String, '/route/player_state', 10
        )
        self.create_service(Trigger, '/route/play', self._play)
        self.create_service(Trigger, '/route/cancel', self._cancel)
        self.create_service(Trigger, '/route/reload', self._reload)

        self.route_data: dict = {}
        self._load_route(log_missing=False)
        self._publish_state('idle')

    def _load_route(self, log_missing: bool = True) -> None:
        if not self.route_file.exists():
            self.route_data = {}
            if log_missing:
                raise FileNotFoundError(self.route_file)
            self.get_logger().warning(
                f'Route file does not exist yet: {self.route_file}'
            )
            return

        data = yaml.safe_load(self.route_file.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('route YAML root must be a mapping')
        points = data.get('points')
        if not isinstance(points, list) or not points:
            raise ValueError('route YAML has no points')
        for index, point in enumerate(points):
            if not isinstance(point, dict):
                raise ValueError(f'route point {index} is not a mapping')
            for key in ('x', 'y', 'yaw'):
                if key not in point:
                    raise ValueError(f'route point {index} has no {key}')

        self.route_data = data
        self.get_logger().info(
            f'Loaded route with {len(points)} points: {self.route_file}'
        )

    def _reload(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self.goal_handle is not None:
            response.success = False
            response.message = 'Cannot reload while route is active'
            return response
        try:
            self._load_route()
        except Exception as exc:
            response.success = False
            response.message = f'Route reload failed: {exc}'
            return response
        response.success = True
        response.message = (
            f"Loaded {len(self.route_data['points'])} route points"
        )
        return response

    def _play(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self.goal_handle is not None or self.state in ('sending', 'running'):
            response.success = False
            response.message = 'A route is already active'
            return response

        try:
            self._load_route()
        except Exception as exc:
            response.success = False
            response.message = f'Cannot load route: {exc}'
            return response

        if not self.action_client.wait_for_server(
            timeout_sec=self.server_wait_timeout
        ):
            response.success = False
            response.message = 'Nav2 navigate_through_poses server is unavailable'
            return response

        frame_id = str(self.route_data.get('frame_id', 'map'))
        now = self.get_clock().now().to_msg()
        poses = [
            self._make_pose(frame_id, now, point)
            for point in self.route_data['points']
        ]

        goal = NavigateThroughPoses.Goal()
        goal.poses = poses

        self._publish_state('sending')
        future = self.action_client.send_goal_async(
            goal, feedback_callback=self._feedback
        )
        future.add_done_callback(self._goal_response)

        response.success = True
        response.message = f'Sending {len(poses)} route points to Nav2'
        return response

    def _goal_response(self, future) -> None:
        try:
            goal_handle = future.result()
        except Exception:
            self.goal_handle = None
            self._publish_state('error')
            self.get_logger().exception('Failed to send route goal')
            return

        if not goal_handle.accepted:
            self.goal_handle = None
            self._publish_state('rejected')
            self.get_logger().error('Nav2 rejected the route')
            return

        self.goal_handle = goal_handle
        self._publish_state('running')
        self.get_logger().info('Nav2 accepted the route')
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(self._result)

    def _feedback(self, feedback_message) -> None:
        feedback = feedback_message.feedback
        poses_remaining = getattr(feedback, 'number_of_poses_remaining', None)
        if poses_remaining is not None:
            self.get_logger().debug(
                f'Route poses remaining: {poses_remaining}'
            )

    def _result(self, future) -> None:
        try:
            wrapped_result = future.result()
            status = wrapped_result.status
        except Exception:
            self._publish_state('error')
            self.get_logger().exception('Route action failed')
        else:
            if status == 4:
                self._publish_state('completed')
                self.get_logger().info('Route completed')
            elif status == 5:
                self._publish_state('canceled')
                self.get_logger().warning('Route canceled')
            else:
                self._publish_state(f'failed:{status}')
                self.get_logger().error(
                    f'Route finished with action status {status}'
                )
        finally:
            self.goal_handle = None

    def _cancel(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self.goal_handle is None:
            response.success = False
            response.message = 'No active route to cancel'
            return response

        self.goal_handle.cancel_goal_async()
        response.success = True
        response.message = 'Route cancellation requested'
        self._publish_state('canceling')
        return response

    def _publish_state(self, state: str) -> None:
        self.state = state
        message = String()
        message.data = state
        self.state_publisher.publish(message)

    @staticmethod
    def _make_pose(
        frame_id: str, stamp, point: dict[str, float]
    ) -> PoseStamped:
        yaw = float(point['yaw'])
        pose = PoseStamped()
        pose.header.frame_id = frame_id
        pose.header.stamp = stamp
        pose.pose.position.x = float(point['x'])
        pose.pose.position.y = float(point['y'])
        pose.pose.orientation.z = math.sin(yaw / 2.0)
        pose.pose.orientation.w = math.cos(yaw / 2.0)
        return pose


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = RoutePlayerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
