import ast

with open("webapp/app.py", "r") as f:
    lines = f.readlines()

with open("webapp/app.py", "r") as f:
    content = f.read()

tree = ast.parse(content)

# We want to identify the start and end line of functions to remove
to_remove_lines = set()

for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        uses_templates = False
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and child.attr == "TemplateResponse":
                uses_templates = True
                break
            if isinstance(child, ast.Name) and child.id == "templates":
                uses_templates = True
                break
        
        if uses_templates:
            # We must also remove the decorators that are on previous lines if they were commented out.
            # But the AST only knows the lines of the function definition itself.
            start_line = node.lineno - 1
            # If the line before start_line starts with `# @app.get`, we remove it too
            while start_line > 0 and lines[start_line - 1].strip().startswith("# @app.get"):
                start_line -= 1
                
            end_line = node.end_lineno
            
            for i in range(start_line, end_line):
                to_remove_lines.add(i)

# Also remove the `templates = Jinja2Templates(...)` assignment
for node in tree.body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "templates":
                for i in range(node.lineno - 1, node.end_lineno):
                    to_remove_lines.add(i)

# We must also remove `from fastapi.templating import Jinja2Templates`
for node in tree.body:
    if isinstance(node, ast.ImportFrom):
        if node.module == "fastapi.templating":
            for i in range(node.lineno - 1, node.end_lineno):
                to_remove_lines.add(i)

new_lines = []
for i, line in enumerate(lines):
    if i not in to_remove_lines:
        new_lines.append(line)

with open("webapp/app.py", "w") as f:
    f.writelines(new_lines)

print(f"Removed {len(to_remove_lines)} lines.")
