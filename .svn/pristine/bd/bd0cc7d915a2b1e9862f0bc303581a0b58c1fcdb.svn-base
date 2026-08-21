# 后端Agent手册制作所用图片

## 进入网址以后的界面
![alt text](image-69.png)

## AI表格处理助手的操作
- 1、查询功能
**查询建筑炼金台的所有属性**
![alt text](image.png)

**查询神通能力名称为再生的神通描述**：结果会显示多个匹配，可以通过点击切换了看到具体的内容
![alt text](image-1.png)
点击“高级再生”会显示其相应的信息:
![alt text](image-2.png)

- 2、修改功能
**将灵兽蕊花仙的体力资质修改为4443**
![alt text](image-3.png)

**将人物男角色的面板物理攻击修改为5554**
![alt text](image-4.png)

- 3、新增功能
**新增活动名称为新周年庆，id是10006，活动开始时间是2026/5/21**
![alt text](image-5.png)

**新增法宝名称为测试法宝4，法宝描述为TEST**
![alt text](image-6.png)

- 4、删除功能
**删除活动新周年庆**
![alt text](image-7.png)

**删除法宝名称为测试法宝4**
![alt text](image-8.png)


- 5、复合指令
**增加建筑名称为瞭望塔v2，建筑类型为654**
![alt text](image-9.png)

**将门派名称为火法的大世界model_id改为1028，战斗model_id改为1068**
![alt text](image-10.png)
在第一次定位表格出错后可以选择搜索其他表格(耗时相应变长)，定位成功后会返回相应的信息；

- 6、表格型增加
输入**增加灵兽**，可以定位相关位置，弹出待填表格:
![alt text](image-11.png)
在填写相关内容以后可以选择校验或者确认新增，确认新增时会经过一次校验，无误后会自动将表格合并到主表中
![alt text](image-12.png)

- 7、智能问答
**建筑相关的表格有哪些**
![alt text](image-13.png)

**item表格有哪些列**
![alt text](image-14.png)

**邮件相关的表格有哪些**
![alt text](image-15.png)

- 8、对于公式相关的表格:
**将formula_basic表格中的段位名称为测试段位6的基础分修改为600**
![alt text](image-30.png)
![alt text](image-31.png)

**将formula_sum中赛季编号是7的行进行删除**
![alt text](image-32.png)
观察表格发现删除后最终的公式结果没有出错
![alt text](image-33.png)

- 9、批量操作
**查询建筑瞭望塔v2的所有信息；将建筑瞭望塔v2的地图图标修改为4444，代码中建筑实体类型名为CityTest；查询建筑瞭望塔v2的所有信息；删除灵兽玄武龟v2**
![alt text](image-16.png)
![alt text](image-66.png)
![alt text](image-67.png)
![alt text](image-68.png)

## 表格Merge操作比对合并
点击“版本比对”进入初始界面：
![alt text](image-39.png)

可以点击从merge文件夹加载或者选择本地文件比对来进入比对，后续项目会基于文件夹进行选择，暂时用merge文件夹进行代替，在选取完文件以后进行比对完可以进入界面:
![alt text](image-40.png)


可以通过点击“全部sheet”、“仅冲突”、“仅增删改”来筛选Sheet
![alt text](image-41.png)

在选择其中一个sheet以后可以选择展示全部、冲突、修改、新增、删除这些内容
![alt text](image-20.png)
![alt text](image-21.png)

点击出现冲突的单元格可以进入冲突页面，该单元格所出现冲突的列拼接而成的表格，其会显示当前处理的冲突：
![alt text](image-22.png)
然后可以通过将鼠标移动向当前处理行你要选择的值的位置进行选择
![alt text](image-23.png)
也可以点击“整行用此版本”来全部选择采取这个版本的值
![alt text](image-24.png)

在选择解决其中一个冲突后页面会去掉那个已解决的冲突，仅显示剩余的冲突
![alt text](image-25.png)

若刚才操作出错可以点击"已解决"查看已经处理的冲突跳转回去重新选择
![alt text](image-26.png)

merge的时候出现编号冲突会自动根据版本先后合并，编号会自动改为目前最大编号+1,同时会将表格根据主键(通常是第一列的id)，进行sort排序
![alt text](image-28.png)

在所有出现冲突的sheet解决完冲突以后导出全部表格会显示绿色，代表可以导出合并后的版本，导出后会在merge文件夹下新增文件，下载栏也会有新增文件。
![alt text](image-27.png)

**三阶段引导合并**
首先点击“三阶段引导合并”，进入初始界面,选择其中一个生产者分支;
![alt text](image-42.png)
- 第一阶段：本地合并提交
进入界面后会显示各个表格，在右边选择是否参与合并，选择基准版本和勾选衍生文件，在点击"合并此分组"
![alt text](image-43.png)
进入到上面讲述的比对界面进行冲突处理后点击右上方的"产生中间版本"后回到原界面；
![alt text](image-44.png)

在合并完需要提交的表格后，点击右下角“合并完成，进入阶段2”
![alt text](image-45.png)

也可以选择切换生产者来继续阶段1：
![alt text](image-46.png)

- 第二阶段：跨生产者综合
该阶段模拟多个不同来源的提交进行合并，同样进行提交的比对之后生成跨生产者综合的中间版本
![alt text](image-47.png)

- 第三阶段：逐分组合回trunk
将跨生产者综合的中间版与trunk进行比对，因为可能生产者base的trunk不是当前的trunk
![alt text](image-48.png)

点击确认合回全部以后会跳出修改清单
![alt text](image-49.png)
确认和会全部以后就进入结算页面
![alt text](image-50.png)

## 表格浏览功能
进入表格浏览的初始界面
![alt text](image-29.png)

点击具体表格以后能够看到表格的相应信息：
![alt text](image-34.png)

在双击表体中的单元格可以进行单元格的编辑：
![alt text](image-35.png)
修改后确认就完成对于表格单元格的编辑：
![alt text](image-36.png)

可以选择点击页面右上角的快速编辑实现整个表体的快速编辑：
![alt text](image-37.png)
![alt text](image-38.png)

在页面上方有搜索框，可以键入需要搜索的关键词来进行搜索，但搜索总体消耗时间长
