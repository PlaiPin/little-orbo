cmake_minimum_required(VERSION 3.8)
project(orbo_embedded_interfaces)
find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/ControlSession.msg"
  "msg/WheelCommand.msg"
  "msg/FlipperCommand.msg"
  "msg/WheelState.msg"
  "msg/ActuatorStatus.msg"
  "msg/Health.msg"
)
ament_export_dependencies(rosidl_default_runtime)
ament_package()
