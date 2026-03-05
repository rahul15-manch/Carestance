from sqlalchemy import Column, Integer, String, Float, Text, ForeignKey, JSON, Boolean, DateTime
from sqlalchemy.orm import relationship
from .database import Base

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    full_name = Column(String)
    contact_number = Column(String)
    profile_photo = Column(String, nullable=True)
    role = Column(String, default="student")
    
    assessment = relationship("AssessmentResult", back_populates="user", uselist=False)

class AssessmentResult(Base):
    __tablename__ = "assessment_results"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    phase_2_category = Column(String)
    personality = Column(String)
    goal_status = Column(String)
    confidence = Column(Float)
    reasoning = Column(Text)
    raw_answers = Column(JSON)
    
    # Phase 1 (Class Selection)
    selected_class = Column(String, nullable=True) # "10", "12", "Above 12"

    # Phase 3 Fields
    phase3_result = Column(String, nullable=True)
    phase3_answers = Column(JSON, nullable=True)
    phase3_analysis = Column(Text, nullable=True)

    # Phase 4 (Final Stream Assessment)
    final_answers = Column(JSON, nullable=True) # Stores raw a/b/c/d answers
    stream_scores = Column(JSON, nullable=True) # Stores {"PCM": 10, "COMM": 8...}
    recommended_stream = Column(String, nullable=True) # e.g. "Science (PCM)"
    final_analysis = Column(Text, nullable=True) # Detailed AI reasoning
    stream_pros = Column(JSON, nullable=True) # List of strings
    stream_cons = Column(JSON, nullable=True) # List of strings
    
    user = relationship("User", back_populates="assessment")

from sqlalchemy import DateTime
from sqlalchemy.sql import func


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    sender = Column(String) # "user" or "ai"
    content = Column(Text)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="messages")

class Feedback(Base):
    __tablename__ = "feedbacks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    content = Column(Text)
    rating = Column(Integer)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="feedbacks")

class Ticket(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    subject = Column(String)
    description = Column(Text)
    status = Column(String, default="Open")
    admin_reply = Column(Text, nullable=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="tickets")

class CareerPath(Base):
    __tablename__ = "career_paths"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    career_title = Column(String)
    path_data = Column(JSON) # Detailed path steps
    reminders = Column(JSON) # List of reminders/milestones
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="career_paths")

# Update User model to include messages, feedback, and career paths relationships
User.messages = relationship("ChatMessage", back_populates="user", order_by="ChatMessage.timestamp")
User.feedbacks = relationship("Feedback", back_populates="user", order_by="Feedback.timestamp")
User.tickets = relationship("Ticket", back_populates="user", order_by="Ticket.timestamp")
User.career_paths = relationship("CareerPath", back_populates="user", order_by="CareerPath.created_at.desc()")

class CounsellorProfile(Base):
    __tablename__ = "counsellor_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    fee = Column(Float, default=0.0)
    # Storing availability as JSON. E.g., {"Monday": ["10:00", "11:00"], "Tuesday": []}
    availability = Column(JSON, nullable=True)
    account_details = Column(JSON, nullable=True) # e.g. {"bank_name": "...", "account_num": "...", "ifsc": "...", "upi": "..."}
    certificates = Column(JSON, nullable=True) # List of file paths
    experience = Column(Text, nullable=True)
    is_verified = Column(Boolean, default=False)
    verification_status = Column(String, default="pending") # pending, approved, rejected
    tnc_accepted = Column(Boolean, default=False)
    tnc_accepted_at = Column(DateTime, nullable=True)
    is_blocked = Column(Boolean, default=False)
    block_reason = Column(String, nullable=True)

    user = relationship("User", back_populates="counsellor_profile")

class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id"))
    counsellor_id = Column(Integer, ForeignKey("users.id"))
    appointment_time = Column(DateTime)
    status = Column(String, default="scheduled")  # scheduled, completed, cancelled
    payment_status = Column(String, default="pending")  # pending, paid
    meeting_link = Column(String, nullable=True)
    razorpay_order_id = Column(String, nullable=True)
    razorpay_payment_id = Column(String, nullable=True)

    # Join tracking
    counsellor_joined = Column(Boolean, default=False)
    joined_at = Column(DateTime, nullable=True)

    student = relationship("User", foreign_keys=[student_id], back_populates="student_appointments")
    counsellor = relationship("User", foreign_keys=[counsellor_id], back_populates="counsellor_appointments")

User.counsellor_profile = relationship("CounsellorProfile", back_populates="user", uselist=False)
User.student_appointments = relationship("Appointment", foreign_keys="Appointment.student_id", back_populates="student")
User.counsellor_appointments = relationship("Appointment", foreign_keys="Appointment.counsellor_id", back_populates="counsellor")

