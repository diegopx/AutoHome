cmake_minimum_required (VERSION 2.6)
project (AutoHome-Mosquitto-Auth)

include(CheckCCompilerFlag)
include(CheckCXXCompilerFlag)

function(add_c_flag flag)
  string(FIND "${CMAKE_C_FLAGS}" flag alreadythere)
  if (alreadythere EQUAL -1)
    check_c_compiler_flag("${flag}" supported)
    if (supported)
      set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} ${flag}" PARENT_SCOPE)
    endif()
  endif()
endfunction()

function(add_cxx_flag flag)
  string(FIND "${CMAKE_CXX_FLAGS}" flag alreadythere)
  if (alreadythere EQUAL -1)
    check_cxx_compiler_flag("${flag}" supported)
    if (supported)
      set(CMAKE_CXX_FLAGS "${CMAKE_CXX_FLAGS} ${flag}" PARENT_SCOPE)
    endif()
  endif()
endfunction()

set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

set(CMAKE_C_COMPILER color-gcc)
set(CMAKE_CXX_COMPILER color-g++)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

add_c_flag("-Wall")
#add_c_flag("-Wl,--no-undefined")

add_cxx_flag("-Wall")
#add_cxx_flag("-Wl,--no-undefined")

include_directories("${CMAKE_SOURCE_DIR}/dep/include")
link_directories("${CMAKE_SOURCE_DIR}/dep/lib")

add_custom_target(doc ALL
                  COMMAND doxygen Doxyfile
                  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}/doc
                  VERBATIM)


add_library(ah-auth-plugin SHARED "src/auth-plugin.c" "dep/src/sha2.c")
target_link_libraries(ah-auth-plugin "sqlite3")
