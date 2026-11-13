"""Loads a CloudFormation YAML template into plain Python data, with the short-form intrinsic functions
(!Ref, !Sub, !Join, ...) turned into their long JSON form ({"Ref": ...}, {"Fn::Sub": ...}, ...)."""

import yaml


class CfnLoader(yaml.SafeLoader):
    pass


def _intrinsic(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    if tag_suffix in ("Ref", "Condition"):
        return {tag_suffix: value}
    if tag_suffix == "GetAtt" and isinstance(value, str):
        value = value.split(".", 1)
    return {"Fn::" + tag_suffix: value}


CfnLoader.add_multi_constructor("!", _intrinsic)


def load(path):
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=CfnLoader)
