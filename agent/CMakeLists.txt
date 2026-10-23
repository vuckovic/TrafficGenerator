cmake_minimum_required(VERSION 3.20)
project(tgen_agent LANGUAGES CXX VERSION 0.3.2)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()
add_compile_options(-Wall -Wextra -Wpedantic)

find_package(Threads REQUIRED)
find_path(ASIO_INCLUDE asio.hpp REQUIRED)            # libasio-dev (header-only)
find_package(nlohmann_json 3 REQUIRED)               # nlohmann-json3-dev (header-only)

# cpp-httplib 0.18.3, single header vendored in third_party/ (MIT): keeps the agent binary self-contained
set(HTTPLIB_INCLUDE ${CMAKE_CURRENT_SOURCE_DIR}/third_party/httplib)

add_library(tgen_core STATIC src/bgp.cpp src/plan.cpp src/router.cpp src/run.cpp src/garp.cpp)
target_include_directories(tgen_core PUBLIC include ${ASIO_INCLUDE})
target_compile_definitions(tgen_core PUBLIC ASIO_STANDALONE ASIO_NO_DEPRECATED)
target_link_libraries(tgen_core PUBLIC Threads::Threads nlohmann_json::nlohmann_json)

add_executable(tgen-agent src/main.cpp src/serve.cpp)
target_include_directories(tgen-agent PRIVATE ${HTTPLIB_INCLUDE})
target_link_libraries(tgen-agent PRIVATE tgen_core)
# self-contained binary: same OS on all nodes, but avoid libstdc++ drift
target_link_options(tgen-agent PRIVATE -static-libstdc++ -static-libgcc)
target_compile_definitions(tgen-agent PRIVATE TGEN_VERSION="${PROJECT_VERSION}")

option(TGEN_TESTS "Build unit tests" ON)
if(TGEN_TESTS)
  find_package(GTest)
  if(GTest_FOUND)
    enable_testing()
    add_executable(tgen-tests tests/test_bgp.cpp tests/test_plan.cpp)
    target_link_libraries(tgen-tests PRIVATE tgen_core GTest::gtest_main)
    include(GoogleTest)
    gtest_discover_tests(tgen-tests)
  else()
    message(STATUS "GTest not found - unit tests disabled")
  endif()
endif()

install(TARGETS tgen-agent RUNTIME DESTINATION bin)
