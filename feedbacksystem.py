from fastapi import FastAPI, BackgroundTasks, HTTPException,Request
from pydantic import BaseModel
import re
import requests
import httpx
import uvicorn
import os
import base64

# SQLAlchemy imports for database handling
from sqlalchemy import create_engine, Column, Integer, String, Text, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# loading environment variables
CONSUMER_KEY = os.environ.get(${{MPESA_CONSUMER_KEY}})
CONSUMER_SECRET = os.environ.get(${{MPESA_CONSUMER_SECRET}})
SHORTCODE = os.environ.get(${{MPESA_SHORTCODE}})
CONFIRMATION_URL = os.environ.get(${{CONFIRMATION_URL}})
# VALIDATION_URL = os.environ.get(${{VALIDATION_URL}})
TOKEN_URL = os.environ.get(${{TOKEN_URL}},"https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials")
REGISTER_URL = os.environ.get(${{REGISTER_URL}})


# --- Database Setup ---
DATABASE_URL = "sqlite:///./feedback.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()



# Define Customer and Feedback models
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True, index=True)
    first_name = Column(String, index=True)
    second_name = Column(String, index=True)
    last_name = Column(String, nullable=True)
    phone = Column(String, unique=True, index=True)

class Feedback(Base):
    __tablename__ = "feedback"
    id = Column(Integer, primary_key=True, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    rating = Column(Integer, nullable=True)
    comments = Column(Text, nullable=True)

Base.metadata.create_all(bind=engine)


app = FastAPI()


class PaymentPayload(BaseModel):
    TransID: str
    TransTime: str
    TransAmount: str
    BusinessShortCode: str
    BillRefNumber: str
    MSISDN: str
    FirstName: str
    MiddleName: str = ""
    LastName: str = ""



# Utility function to parse the payment confirmation message.
def parse_payment_json(data: dict):
    transaction_id = data.TransID
    firstname = data.FirstName
    secondname = data.MiddleName or ""
    lastname = data.LastName or ""
    phone = data.MSISDN
    dt = data.TransTime
    
    return transaction_id, firstname, secondname, lastname, phone

# function to get access token
def get_access_token():
    try:
        auth = f"{CONSUMER_KEY}:{CONSUMER_SECRET}"
        encoded_auth = base64.b64encode(auth.encode()).decode()
        headers = {"Authorization":f"Basic {encoded_auth}"}
        response = requests.get(TOKEN_URL,headers=headers,timeout=10)
        response.raise_for_status()
        return response.json().get("access_token")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500,detail="failed to authenticate with Mpesa API")

# function to register confirmation and validation url
def register_confirmation_url():
    try:
        token = get_access_token()
        headers = {"Authorization":"Bearer {token}","Content-Type":"Application/json"}
        data = {
            "ShortCode":SHORTCODE,
            "ResponeType":"Completed",
            "ConfirmationURL":CONFIRMATION_URL
        }
        response = requests.post(REGISTER_URL,json=data,headers=headers,timeout=10)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500,detail="failed to register confirmation url")


# Function to send WhatsApp message using the WhatsApp Business API.
async def send_whatsapp_message(phone: str, firstname: str):
    url = "https://graph.facebook.com/v14.0/506280399227577/messages"  
    headers = {
        "Authorization": "Bearer EAAblZC0HBtUEBO6RFFSGqiycZBQ2iA3eTcLEadS5H21E8X2pQ2RhrzcYA17KcOvDWYUI6ZCYEnVzEdgcUEZCeQjDnduilnUDBtRhx4Uet7CGc3sTl9hqrHOIzXYO7xqnC8ALVFSs3RKJdrwa3XcMCYtjafFK4jPZAyiamr4IDNdn5X3Buksu5VslLJ56SAK9tjr7gnu1r96KKlZB1ISE7weEqZAzyUZD",  
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {
            "body": (
                f"Hi {firstname}, thank you for your payment! "
                "Could you please rate our service on a scale of 1 to 5? "
                "Also, let us know if you're comfortable providing additional feedback about our business."
            )
        }
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        if response.status_code != 200:
            # Log the error or implement retry logic as needed
            print(f"Failed to send WhatsApp message: {response.text}")

@app.on_event("startup")
async def startup_event():
    # Check if critical environment variables are set
    if not all([CONSUMER_KEY, CONSUMER_SECRET, SHORTCODE, CONFIRMATION_URL, REGISTER_URL, TOKEN_URL]):
        print("WARNING: Some critical environment variables are not set!")
        return
    response = register_confirmation_url()

# Payment confirmation endpoint
@app.post("/payment-confirmation")
async def payment_confirmation(payload: PaymentPayload, background_tasks: BackgroundTasks):
    transaction_id, firstname, secondname, lastname, phone = parse_payment_json(payload)
    if not firstname or not phone:
        raise HTTPException(status_code=400, detail="Invalid payment message format. Required fields not found.")

    # Save the customer information in the database 
    db: Session = SessionLocal()
    customer = db.query(Customer).filter(Customer.phone == phone).first()
    if not customer:
        customer = Customer(first_name=firstname, second_name=secondname, last_name=lastname, phone=phone)
        db.add(customer)
        db.commit()
        db.refresh(customer)
    db.close()

    # Trigger the WhatsApp feedback message immediately via a background task
    background_tasks.add_task(send_whatsapp_message, phone, firstname)

    return {"status": "Payment confirmed and feedback request sent."}

# Endpoint to store feedback responses in the database.
class FeedbackResponse(BaseModel):
    phone: str
    rating: int
    comments: str = None

@app.post("/store-feedback")
async def store_feedback(feedback: FeedbackResponse):
    db: Session = SessionLocal()
    # Locate the customer by phone number
    customer = db.query(Customer).filter(Customer.phone == feedback.phone).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found.")
    
    new_feedback = Feedback(customer_id=customer.id, rating=feedback.rating, comments=feedback.comments)
    db.add(new_feedback)
    db.commit()
    db.refresh(new_feedback)
    db.close()
    return {"status": "Feedback stored successfully."}

@app.get("/check-database")
async def check_database():
    db: Session = SessionLocal()
    customers = db.query(Customer).all()
    data = []
    for customer in customers:
        feedbacks = db.query(Feedback).filter(Feedback.customer_id == customer.id).all()
        data.append({
            "customer_id": customer.id,
            "first_name": customer.first_name,
            "second_name": customer.second_name,
            "last_name": customer.last_name,
            "phone": customer.phone,
            "feedback": [
                {
                    "feedback_id": fb.id,
                    "rating": fb.rating,
                    "comments": fb.comments
                } for fb in feedbacks
            ]
        })
    db.close()
    return {"data": data}


@app.get('/')
def home():
    return {
        "message": "the route is working",
    }



if __name__ == "__main__":
    uvicorn.run("feedbacksytem:app", host="0.0.0.0", port=8000, log_level="info")
    
