# ApiHubManager
API管理系统
通过连接 数据源 MySQL  SQLSERVER pgSQL  即可通过点击鼠标  形成可发布的 API 接口！

## 一、Docker Compose 部署（推荐，自带 MySQL + Redis）

env.txt 文件更改为.env  并自行修改里面的密钥
dockerignore  文件更改为.dockerignore
保存！

##  安装好 docker  compose   (步骤省略)   将API_Hub_MANAGER 上传到服务器 
```bash
cd api_manager
docker compose up -d --build      # --build 必须带（依赖变了要重建镜像）
docker compose ps                 # 三个服务都 healthy 即成功
```



### 4. 验证
浏览器打开 `http://<服务器IP>:8000/admin`，首次进入初始化管理员口令页。
服务器防火墙放行 8000 端口：`sudo firewall-cmd --add-port=8000/tcp --permanent && sudo firewall-cmd --reload`
docker compose logs -f app        # 看程序日志


<img width="2560" height="999" alt="1" src="https://github.com/user-attachments/assets/241e7642-54e2-4b2a-b23d-1f6369761692" />
<img width="2298" height="1148" alt="2" src="https://github.com/user-attachments/assets/1a0c8d55-193c-48b6-94f7-3381786dfd68" />
<img width="2249" height="1121" alt="3" src="https://github.com/user-attachments/assets/be4db9b9-4754-45d2-9126-0c824b822b35" />
<img width="2484" height="1200" alt="4" src="https://github.com/user-attachments/assets/541cf986-879c-462b-a157-53d13822bf96" />
<img width="2528" height="1168" alt="5" src="https://github.com/user-attachments/assets/bd541fd2-f120-4b54-b6fb-59c75828907d" />
<img width="2532" height="819" alt="6" src="https://github.com/user-attachments/assets/efb5c8fa-1144-489b-953d-5f4233731a01" />
<img width="2560" height="738" alt="7" src="https://github.com/user-attachments/assets/cbe48073-a0a0-4430-b7b4-28478700eb74" />




