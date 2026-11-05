cmake_minimum_required(VERSION 3.28)

get_filename_component(customHostDefaultGameHub "${CMAKE_CURRENT_LIST_DIR}/../.." ABSOLUTE)
set(GAMEHUB_SOURCE_DIR "${customHostDefaultGameHub}" CACHE PATH "GameHub 公共源码目录")
# 独立宿主的顶层目录不含依赖清单；在 project() 加载工具链前指向公共清单。
if(NOT DEFINED VCPKG_MANIFEST_DIR)
    set(VCPKG_MANIFEST_DIR "${GAMEHUB_SOURCE_DIR}" CACHE PATH "依赖清单目录")
endif()

project(SyntheticGameHubHost LANGUAGES CXX)

if(NOT TARGET GameHub::service)
    if(NOT EXISTS "${GAMEHUB_SOURCE_DIR}/src/service/Application.h")
        message(FATAL_ERROR "GAMEHUB_SOURCE_DIR 必须指向完整的 GameHub 公共源码目录")
    endif()
    set(GAMEHUB_BUILD_APP OFF CACHE BOOL "由宿主提供入口" FORCE)
    set(GAMEHUB_BUILD_TESTS OFF CACHE BOOL "宿主示例不构建公共测试" FORCE)
    set(GAMEHUB_ENABLE_INSTALL OFF CACHE BOOL "宿主自行决定发行内容" FORCE)
    add_subdirectory("${GAMEHUB_SOURCE_DIR}" "${CMAKE_CURRENT_BINARY_DIR}/gamehub" EXCLUDE_FROM_ALL)
endif()

add_executable(synthetic_gamehub_host main.cpp)
target_compile_features(synthetic_gamehub_host PRIVATE cxx_std_20)
target_link_libraries(synthetic_gamehub_host PRIVATE GameHub::service GameHub::bootstrap)
if(MSVC)
    target_compile_options(synthetic_gamehub_host PRIVATE /W4 /utf-8 /permissive- /bigobj /EHsc /Zc:preprocessor /Zc:__cplusplus)
else()
    target_compile_options(synthetic_gamehub_host PRIVATE -Wall -Wextra -Wpedantic)
endif()
