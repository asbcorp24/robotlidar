cmake_minimum_required(VERSION 3.16)
project(radxa_zero3w_stereo LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_executable(radxa_stereo_node
    src/main.cpp
    src/ptz.cpp
    src/pwm_sysfs.cpp
    src/video_pipeline.cpp
)

target_include_directories(radxa_stereo_node PRIVATE include)
target_compile_options(radxa_stereo_node PRIVATE -Wall -Wextra -Wpedantic)

target_link_libraries(radxa_stereo_node PRIVATE pthread)

install(TARGETS radxa_stereo_node RUNTIME DESTINATION bin)
