from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
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
        consumer_key = os.environ.get('MPESA_CONSUMER_KEY')
        consumer_secret = os.environ.get('MPESA_CONSUMER_SECRET')
        
        # Debug credential presence
        print(f"Consumer key exists: {bool(consumer_key)}")
        print(f"Consumer secret exists: {bool(consumer_secret)}")
        
        if not consumer_key or not consumer_secret:
            print("Error: M-Pesa credentials not found in environment variables")
            raise HTTPException(status_code=500, detail="M-Pesa credentials not configured properly")
        
        # Create the auth string and encode it
        auth_string = f"{consumer_key}:{consumer_secret}"
        auth_bytes = auth_string.encode('ascii')
        auth_base64 = base64.b64encode(auth_bytes).decode('ascii')
        
        # Set up headers with the correct Authorization format
        headers = {
            "Authorization": f"Basic {auth_base64}",
            "Content-Type": "application/json"
        }
        
        # Make the API request
        url = "https://api.safaricom.co.ke/oauth/v1/generate"  
        params = {'grant_type': 'client_credentials'}
        
        print(f"Making request to: {url}")
        print(f"With headers: Authorization: Basic ***** (redacted)")
        
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=15
        )
        
        print(f"Response status code: {response.status_code}")
        print(f"Response body: {response.text[:100]}...") 
        
        # Check for errors
        response.raise_for_status()
        
        # Parse and return the access token
        data = response.json()
        return data['access_token']
        
    except requests.exceptions.RequestException as e:
        print(f"Authentication error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to authenticate with Mpesa API")
    except KeyError as e:
        print(f"Key error in response: {str(e)}")
        raise HTTPException(status_code=500, detail="Unexpected response format from Mpesa API")
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

# function to register confirmation and validation url
def register_confirmation_url():
    try:
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        shortcode = os.environ.get('MPESA_SHORTCODE')
        confirmation_url = os.environ.get('CONFIRMATION_URL')
        validation_url = os.environ.get('VALIDATION_URL')
        
        if not shortcode or not confirmation_url:
            print("Error: M-Pesa shortcode or confirmation URL not found in environment variables")
            raise HTTPException(status_code=500, detail="M-Pesa configuration not complete")
        
        data = {
            "ShortCode": shortcode,
            "ResponseType": "Completed",  
            "ConfirmationURL": confirmation_url,
            "ValidationURL":validation_url
        }
        
        register_url = "https://api.safaricom.co.ke/mpesa/c2b/v1/registerurl" 
        
        print(f"Registering URL: {register_url}")
        print(f"With data: {data}")
        
        response = requests.post(
            register_url,
            json=data,
            headers=headers,
            timeout=15
        )
        
        print(f"Register URL response status: {response.status_code}")
        print(f"Register URL response: {response.text}")
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Failed to register confirmation URL: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to register confirmation URL")
    except Exception as e:
        print(f"Unexpected error during URL registration: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred")

# Function to send WhatsApp message using the WhatsApp Business API.
async def send_whatsapp_message(phone: str, firstname: str):
    url = "https://graph.facebook.com/v14.0/506280399227577/messages"  
    headers = {
        "Authorization": f"Bearer {os.environ.get('WHATSAPP_API_TOKEN')}",  
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
    required_vars = [
        'MPESA_CONSUMER_KEY', 
        'MPESA_CONSUMER_SECRET', 
        'MPESA_SHORTCODE', 
        'CONFIRMATION_URL',
        'WHATSAPP_API_TOKEN'
    ]
    
    missing_vars = [var for var in required_vars if not os.environ.get(var)]
    
    if missing_vars:
        print(f"WARNING: The following environment variables are missing: {', '.join(missing_vars)}")
        print("The application will attempt to start, but some functionality may not work correctly.")
        # Don't try to register URL if credentials are missing
        if 'MPESA_CONSUMER_KEY' not in missing_vars and 'MPESA_CONSUMER_SECRET' not in missing_vars:
            try:
                response = register_confirmation_url()
                print(f"URL registration successful: {response}")
            except Exception as e:
                print(f"URL registration failed but continuing startup: {e}")
    else:
        try:
            response = register_confirmation_url()
            print(f"URL registration successful: {response}")
        except Exception as e:
            print(f"URL registration failed but continuing startup: {e}")

# Payment confirmation endpoint
@app.post("/payment-confirmation")
async def payment_confirmation(payload: PaymentPayload, background_tasks: BackgroundTasks):
    transaction_id, firstname, secondname, lastname, phone = parse_payment_json(payload)
    if not firstname or not phone:
        raise HTTPException(status_code=400, detail="Invalid payment message format. Required fields not found.")

    # Save the customer information in the database 
    db: Session = SessionLocal()
    try:
        customer = db.query(Customer).filter(Customer.phone == phone).first()
        if not customer:
            customer = Customer(first_name=firstname, second_name=secondname, last_name=lastname, phone=phone)
            db.add(customer)
            db.commit()
            db.refresh(customer)
    except Exception as e:
        db.rollback()
        print(f"Database error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to store customer data")
    finally:
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
    try:
        # Locate the customer by phone number
        customer = db.query(Customer).filter(Customer.phone == feedback.phone).first()
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found.")
        
        new_feedback = Feedback(customer_id=customer.id, rating=feedback.rating, comments=feedback.comments)
        db.add(new_feedback)
        db.commit()
        db.refresh(new_feedback)
        return {"status": "Feedback stored successfully."}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"Database error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to store feedback")
    finally:
        db.close()

@app.get("/check-database")
async def check_database():
    db: Session = SessionLocal()
    try:
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
        return {"data": data}
    except Exception as e:
        print(f"Database error: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to retrieve database records")
    finally:
        db.close()

@app.get('/')
def home():
    return {
        "message": "The API is working",
        "status": "online",
        "version": "1.0.0"
    }

# Health check endpoint
@app.get('/health')
def health_check():
    return {
        "status": "healthy",
        "database": "connected" if engine else "not connected"
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("feedbacksystem:app", host="0.0.0.0", port=port, log_level="info")
