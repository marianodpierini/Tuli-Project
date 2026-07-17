def remove_prefix(codigo, config):
    value = config.get("value", "")
    if codigo.startswith(value):
        return codigo[len(value):]
    return codigo



PARSERS_DICT = {
    "remove_prefix": remove_prefix,
}