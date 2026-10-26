include(GNUInstallDirs)
include(CMakePackageConfigHelpers)
find_package(Threads REQUIRED)
if(GC_HAVE_SUITESPARSE)
  message(FATAL_ERROR "The geometry-central recipe installs the Eigen-only profile; set SUITESPARSE=OFF")
endif()

# Internal header-only dependencies are needed in the build tree, not as
# unexported targets in the installed package.
set_property(TARGET geometry-central PROPERTY INTERFACE_INCLUDE_DIRECTORIES
  "$<BUILD_INTERFACE:${PROJECT_SOURCE_DIR}/include>;$<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>;$<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}/geometrycentral/third_party>")
set_property(TARGET geometry-central PROPERTY INTERFACE_LINK_LIBRARIES
  "$<BUILD_INTERFACE:${GC_DEP_LIBS}>;$<INSTALL_INTERFACE:Eigen3::Eigen>")
target_link_libraries(geometry-central PUBLIC Threads::Threads)

install(TARGETS geometry-central EXPORT geometry-centralTargets
  ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
  LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
  RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(FILES "${PROJECT_SOURCE_DIR}/deps/happly/happly.h"
  DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/geometrycentral/third_party)
install(DIRECTORY "${PROJECT_SOURCE_DIR}/deps/nanort/include/nanort"
  DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/geometrycentral/third_party
  FILES_MATCHING PATTERN "*.h")
install(FILES "${PROJECT_SOURCE_DIR}/deps/nanoflann/include/nanoflann.hpp"
  DESTINATION ${CMAKE_INSTALL_INCLUDEDIR}/geometrycentral/third_party/nanoflann)
install(FILES "${PROJECT_SOURCE_DIR}/LICENSE"
  DESTINATION ${CMAKE_INSTALL_DATADIR}/licenses/geometry-central)

set(_gc_package_dir "${CMAKE_INSTALL_LIBDIR}/cmake/geometry-central")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/geometry-centralConfig.cmake.in" [=[
@PACKAGE_INIT@
include(CMakeFindDependencyMacro)
find_dependency(Eigen3 3.3 CONFIG)
find_dependency(Threads)
include("${CMAKE_CURRENT_LIST_DIR}/geometry-centralTargets.cmake")
check_required_components(geometry-central)
]=])
configure_package_config_file("${CMAKE_CURRENT_BINARY_DIR}/geometry-centralConfig.cmake.in"
  "${CMAKE_CURRENT_BINARY_DIR}/geometry-centralConfig.cmake"
  INSTALL_DESTINATION "${_gc_package_dir}")
install(EXPORT geometry-centralTargets NAMESPACE geometry-central:: DESTINATION "${_gc_package_dir}")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/geometry-centralConfig.cmake" DESTINATION "${_gc_package_dir}")
unset(_gc_package_dir)
