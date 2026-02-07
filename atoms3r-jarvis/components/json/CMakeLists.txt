# Componente wrapper per compatibilità ESP-SR con ESP-IDF 6.x
# ESP-SR richiede 'json' ma in IDF 6.x è stato rinominato 'cjson'
# Questo componente funge da alias/wrapper

idf_component_register(
    REQUIRES cjson
)
