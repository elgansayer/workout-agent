import ast

with open('backend/webapp/app.py', 'r') as f:
    code = f.read()

tree = ast.parse(code)
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef):
        is_route = False
        route_path = ""
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if dec.func.attr in ['get', 'post', 'put', 'delete', 'patch']:
                    is_route = True
                    if dec.args and isinstance(dec.args[0], ast.Constant):
                        route_path = dec.args[0].value

        if is_route:
            # Check if _check_api_auth is called
            has_auth = False
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                    if child.func.id == '_check_api_auth':
                        has_auth = True
                        break
            if not has_auth:
                print(f"Missing auth in {node.name} for {route_path}")
