"""ROS executable adapters for CLIs that also run without a ROS installation."""


def runtime():
    from rclpy.utilities import remove_ros_args
    from .cli import main

    return main(remove_ros_args()[1:])


def audio_check():
    from rclpy.utilities import remove_ros_args
    from .audio_check import main

    return main(remove_ros_args()[1:])
