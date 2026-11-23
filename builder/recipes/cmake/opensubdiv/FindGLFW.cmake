# Use the builder's GLFW target, including its platform libraries (QuartzCore on macOS).
find_package(glfw3 ${GLFW_FIND_VERSION} CONFIG REQUIRED)
set(GLFW_FOUND TRUE)
set(GLFW_VERSION "${glfw3_VERSION}")
set(GLFW_LIBRARIES glfw)
get_target_property(GLFW_INCLUDE_DIR glfw INTERFACE_INCLUDE_DIRECTORIES)
