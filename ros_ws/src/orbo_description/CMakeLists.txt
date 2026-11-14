cmake_minimum_required(VERSION 3.16)
project(orbo_description)
find_package(ament_cmake REQUIRED)
install(DIRECTORY config launch DESTINATION share/${PROJECT_NAME})
install(DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}/../../../cad/meshes/v9"
  DESTINATION share/${PROJECT_NAME}/meshes)
ament_package()
