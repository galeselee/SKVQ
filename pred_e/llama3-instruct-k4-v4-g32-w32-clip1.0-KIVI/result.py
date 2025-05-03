import json

# 假设你的文件名是 result.json
filename = 'result.json'

# 读取 JSON 文件
with open(filename, 'r') as f:
    data = json.load(f)

# 存储每个键的平均值
averages = []

# 遍历每个键，计算对应的平均值
for key, value in data.items():
    # 获取所有区间的值并计算平均值
    averages.append(value)
# 输出平均值
items_num = len(averages)
print(items_num)
sum_value = 0
for avg in averages:
    sum_value += avg
print(sum_value/items_num)
