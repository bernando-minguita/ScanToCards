with open('core/database.py', 'r') as f:
    content = f.read()

content = content.replace('_SCHEMA = """\nCREATE', '_SCHEMA = f"""\nCREATE')

with open('core/database.py', 'w') as f:
    f.write(content)

print('Fixed f-string')