# 重要内容

## Excel-Agent表格处理
已增加可选模型方式：
![alt text](image-14.png)

**新增一个NPC叫'神秘行商'，model_id 1009，放在space_id 10001坐标(80,0,50)，玩家点击后弹出对话'客官里面请，今日新到一批奇珍异宝'，选项'进来瞧瞧'和'下次再来'**
![alt text](image-15.png)
![alt text](image-16.png)
![alt text](image-17.png)


**新增一个NPC叫'酒馆老板'，model_id 1011，放在space_id 10001坐标(70,0,40)，玩家点击后弹出对话'客官打尖还是住店？'，选项'打尖'和'住店'，点'打尖'后老板说'好嘞，酒菜这就端来，见面礼reward_id 10070请笑纳'，再点'多谢'关闭。**
![alt text](image-18.png)


## Merge合并引导


### 分支合并
#### absorb（分支与分支合并）
![alt text](image.png)
选择分支后会得出各自的SVN版本
![alt text](image-1.png)

比对完成以后表格显示如下：
![alt text](image-2.png)
表格部分只展示存在冲突的表格，如果需要查看修改的表格，可以右边《改动表》点击开始展开；
sheet可以点击下拉框展开

- 两种表格展示方式：
**全表单元格**
![alt text](image-3.png)
这种情况冲突会显示在单元格中，然后点击冲突单元格可以出现窗口具体展示冲突，进行选择
![alt text](image-4.png)

**并排两表**
![alt text](image-5.png)
界面展示两个表格，分别展示两个分支与base的差异表格，可以点击其中一个表格的单元格实现冲突解决；

![alt text](image-6.png)


全sheet AI建议：
**全表单元格**会将AI建议显示在单元格中
![alt text](image-7.png)
点击单元格可以看到AI建议：
![alt text](image-8.png)

**并排两表**会在表格体当中用蓝色来标识AI建议的单元格
![alt text](image-9.png)

可以通过在输入置信度以后选择批量采纳高置信来实现采纳AI建议；
![alt text](image-10.png)

完成冲突处理后点击《应用合并》会跳出变更汇总：
![alt text](image-11.png)

确认后会进入commit名称填写和写回选择：
![alt text](image-12.png)

之后提交历史可以记录本次的操作
![alt text](image-13.png)


#### merge_back（合回trunk）
这部分merge_back默认To分支是trunk当前的最新版本，选择分支以后会自动寻找trunk最新版本，得到各自的版本号
![alt text](image-22.png)
![alt text](image-23.png)
比对完成后的表体内容与上文一致
![alt text](image-24.png)

### 目录合并
优先选择To目录，当前表格资源To目录选择trunk分支
![alt text](image-19.png)
trunk分支会在目录下寻找子目录
![alt text](image-20.png)

开始比对以后展示内容与上述的分支合并一致
![alt text](image-21.png)