set(PEER_PROJECT_PATH "../../deps/libpeer")

# Explicit file list — exclude files not needed on ESP32:
#   ssl_transport.c  - MQTT/HTTP signaling transport (not needed)
#   peer_signaling.c - disabled via DISABLE_PEER_SIGNALING
# NOTE: sctp.c IS included — it has a non-USRSCTP fallback used by peer_connection.c
set(CODES
  "${PEER_PROJECT_PATH}/src/address.c"
  "${PEER_PROJECT_PATH}/src/agent.c"
  "${PEER_PROJECT_PATH}/src/base64.c"
  "${PEER_PROJECT_PATH}/src/dtls_srtp.c"
  "${PEER_PROJECT_PATH}/src/ice.c"
  "${PEER_PROJECT_PATH}/src/mdns.c"
  "${PEER_PROJECT_PATH}/src/peer.c"
  "${PEER_PROJECT_PATH}/src/peer_connection.c"
  "${PEER_PROJECT_PATH}/src/ports.c"
  "${PEER_PROJECT_PATH}/src/rtcp.c"
  "${PEER_PROJECT_PATH}/src/rtp.c"
  "${PEER_PROJECT_PATH}/src/sctp.c"
  "${PEER_PROJECT_PATH}/src/sdp.c"
  "${PEER_PROJECT_PATH}/src/socket.c"
  "${PEER_PROJECT_PATH}/src/stun.c"
  "${PEER_PROJECT_PATH}/src/utils.c"
)

idf_component_register(
  SRCS ${CODES}
  INCLUDE_DIRS "${PEER_PROJECT_PATH}/src"
  REQUIRES mbedtls srtp json esp_netif lwip
)

# NOTE: mbedTLS 4.x compat shims (mbedtls_compat/) are NOT needed in IDF 5.5
# which uses mbedTLS 3.x with standard header paths.

# Disable KeepAlives (set to 0 in config.h)
file(READ ${CMAKE_CURRENT_SOURCE_DIR}/../../deps/libpeer/src/config.h INPUT_CONTENT)
string(REPLACE "#define KEEPALIVE_CONNCHECK 10000" "#define KEEPALIVE_CONNCHECK 0" MODIFIED_CONTENT ${INPUT_CONTENT})
file(WRITE ${CMAKE_CURRENT_SOURCE_DIR}/../../deps/libpeer/src/config.h ${MODIFIED_CONTENT})

if(NOT IDF_TARGET STREQUAL linux)
  add_definitions("-DESP32")
endif()

# CONFIG_USE_LWIP=1: use lwIP (ESP32 networking) instead of POSIX ifaddrs
# CONFIG_USE_USRSCTP=0: disable usrsctp (not needed for audio-only WebRTC)
# DISABLE_PEER_SIGNALING: disable MQTT/HTTP signaling
add_definitions("-DCONFIG_USE_LWIP=1")
add_definitions("-DCONFIG_USE_USRSCTP=0")
add_definitions("-DCONFIG_DTLS_USE_ECDSA=1")
add_definitions("-DHTTP_DO_NOT_USE_CUSTOM_CONFIG -DMQTT_DO_NOT_USE_CUSTOM_CONFIG -DDISABLE_PEER_SIGNALING=true")
