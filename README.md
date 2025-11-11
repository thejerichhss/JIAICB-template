## Deployment

### 1. Deploying on Render
Render is a cloud platform that can host web apps and backend services. Follow these steps to deploy this template:

1. **Sign up / Log in**  
   Go to [https://render.com](https://render.com) and create an account or log in.

2. **Create a New Web Service**  
   - Click **New → Web Service**.  
   - Connect your GitHub account and select the `JIAICB-template` repository. (or whatever you made it)

3. **Configure Service**  
   - **Environment:** Python 3 (choose your preferred version, e.g., 3.10).  
   - **Build Command:**  
     ```bash
     pip install -r requirements.txt
     ```  
   - **Start Command:**  
     ```bash
     gunicorn server:app
     ```  
   - **Region:** Choose the region closest to your users.

4. **Environment Variables (Optional)**  
   If your `server.py` uses environment variables (e.g., `PORT`), add them under the **Environment** tab.

5. **Deploy**  
   Click **Create Web Service**. Render will automatically build and start your app. You’ll receive a public URL like `https://jiaicb-template.onrender.com`.

---
