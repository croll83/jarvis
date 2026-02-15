/**
 * Compatibility stub for esp-sr pre-compiled libraries.
 *
 * ESP-IDF v6.1 removed the __getreent function and replaced it with a macro.
 * The esp-sr binary libraries were compiled against an older ESP-IDF and still
 * reference __getreent as an external symbol. This stub provides the symbol.
 */
#include <sys/reent.h>

// Undefine the macro so we can define the function
#undef __getreent

extern struct _reent *_impure_ptr;

__attribute__((used))
struct _reent* __getreent(void) {
    return _impure_ptr;
}
