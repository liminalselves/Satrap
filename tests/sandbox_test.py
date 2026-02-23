from satrap.core.utils.sandbox import CodeSandbox

# 创建沙箱对象，使用当前Python解释器和当前目录下的sandbox文件夹
sandbox = CodeSandbox('./sandbox', "E:\\conda\\envs\\code\\python.exe")

# 运行代码字符串（输出中文测试）
result = sandbox.run('print("你好，世界！")')
print(result['stdout'])

# 保存代码到文件
sandbox.save_to_file('print("Hello from file")', 'subdir/hello.py')
sandbox.save_to_file('print("Another file")', 'another.py')
sandbox.save_to_file('print("Nested file")', 'subdir/deep/inside.py')

# 运行刚才保存的文件
result = sandbox.run_file('subdir/hello.py')
print(result['stdout'])

# 列出沙箱内所有文件（包含目录信息）
print("沙箱内所有文件:")
for f in sandbox.list_files():
    print(f"  {f}")
# 输出示例:
#   another.py
#   subdir/hello.py
#   subdir/deep/inside.py

# 删除一个文件
sandbox.delete_file('another.py')
print("删除文件后列表:")
for f in sandbox.list_files():
    print(f"  {f}")

# 删除一个空目录（先确保为空）
sandbox.save_to_file('# temp', 'empty_dir/placeholder.py')
sandbox.delete_file('empty_dir/placeholder.py')
sandbox.delete_directory('empty_dir', recursive=False)  # 此时 empty_dir 为空，可以删除

# 递归删除非空目录
sandbox.delete_directory('subdir', recursive=True)
print("递归删除 subdir 后文件列表:")
for f in sandbox.list_files():
    print(f"  {f}")
# 输出应为空（只剩可能忽略的其他文件）
